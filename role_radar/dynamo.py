"""DynamoDB state store and lease, sharing one table (single-table design).

  pk            sk                         item
  <company>     <job uid>                  a seen job: the fields of storage.SeenJob
  #schedule     <company>                  its schedule: storage.CompanyMeta
  #lease        #lease                     who may check companies right now (lease.py)
  #alerts       <time>#<company>#<uid>     log of sent alerts for `status`; expire via TTL
  #runs         <runner>                   each runner's last pass, for `status`
  #stats        <hour>#<runner>            a runner's activity counts for one UTC hour; expire via TTL
  #digest       #digest / #request         the digest's schedule / the latest "Send now"
  #queue        <company>#<uid>            a match waiting for the digest, for Live Tracking
  #round        <runner>                   how far the runner's pass in progress has got

Company names can't start with "#" (the config loader rejects them), so they
never collide with the rows the store keeps for itself.

Reads are strongly consistent: straight after a handoff, the new runner must
see everything the previous one wrote. Saving a company is a transaction that
also checks the lease, so the save commits only while this runner still holds
the lease with the epoch it acquired.

The #queue rows are written in the same transaction as the job rows they
mirror, so Live Tracking sees a match as soon as its company is saved. The
user's skip is the one attribute only the app writes: saves update the other
attributes and leave it alone.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from role_radar.lease import Lease, LeaseInfo, LeaseLost
from role_radar.storage import (
    SKIPPED, SKIPPED_KEPT, STATS_KEPT, SWITCHES as SWITCH_NAMES, CompanyMeta, CompanyRecord, DigestSchedule, MonitorState,
    QueuedMatch, SeenJob, StateStore, compact, to_iso,
)

log = logging.getLogger(__name__)

SCHEDULE = "#schedule"
LEASE = "#lease"
ALERTS = "#alerts"
RUNS = "#runs"
DIGEST = "#digest"
SWITCHES = "#switches"
STATS = "#stats"
QUEUE = "#queue"
ROUND = "#round"
REQUEST = "#request"  # sort key of the "Send now" row, next to the digest's schedule
ROUND_TTL = 2 * 86400
ALERT_TTL = 30 * 86400  # seconds an alert-log row lives (the table's TTL attribute is "ttl")
MAX_TRANSACTION = 100  # DynamoDB's limit on actions per TransactWriteItems
TRANSACTION_ATTEMPTS = 11
REQUEST_STALE_AFTER = 120  # seconds a handoff request keeps others from taking the lease
_RETRYABLE_TXN = {"ThrottlingError", "ProvisionedThroughputExceeded", "TransactionConflict"}

_serialize = TypeSerializer().serialize
_deserialize = TypeDeserializer().deserialize


def _key(pk: str, sk: str) -> dict[str, Any]:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _item(pk: str, sk: str, attrs: dict[str, Any]) -> dict[str, Any]:
    # DynamoDB numbers must be Decimals, not floats.
    return {**_key(pk, sk), **{k: _serialize(Decimal(str(v)) if isinstance(v, float) else v) for k, v in attrs.items()}}


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, value in item.items():
        value = _deserialize(value)
        if isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        out[name] = value
    return out


def _waiting(attrs: dict[str, Any] | None) -> bool:
    """storage.waiting() for a stored job row."""
    return bool(attrs and attrs.get("matched") and not attrs.get("notified_at") and not attrs.get("duplicate_of"))


def _num(value: float) -> dict[str, str]:
    return {"N": repr(round(value, 3))}


def _iso(ts: float) -> str:
    return to_iso(datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0))


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _backoff(attempt: int, cap: float = 2.0) -> None:
    time.sleep(min(0.05 * 2**attempt, cap))


def create_table(client: Any, table: str, read_capacity: int = 25, write_capacity: int = 25) -> None:
    """Create the table as the SAM template does (for tests and local experiments)."""
    client.create_table(
        TableName=table,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": read_capacity, "WriteCapacityUnits": write_capacity},
    )
    client.get_waiter("table_exists").wait(TableName=table)
    client.update_time_to_live(TableName=table, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})


class DynamoLease(Lease):
    """The lease as one item. Every change is a conditional update, so two runners can't both win."""

    _NAMES = {"#holder": "holder", "#epoch": "epoch", "#exp": "expires_at"}

    def __init__(self, client: Any, table: str, holder: str, clock: Callable[[], float] = time.time) -> None:
        super().__init__(holder, clock)
        self.client = client
        self.table = table

    def acquire(self, ttl: float) -> bool:
        now = self.clock()
        try:
            resp = self._update(
                "SET #holder = :holder, #exp = :exp, acquired_at = :iso, renewed_at = :iso "
                "REMOVE released_at, requested_by, requested_at ADD #epoch :one",
                # Free, expired or already ours; and nobody else has just asked for a handoff.
                "(attribute_not_exists(pk) OR #exp <= :now OR #holder = :holder) AND "
                "(attribute_not_exists(requested_by) OR requested_by = :holder OR requested_at <= :stale)",
                {
                    ":holder": {"S": self.holder},
                    ":exp": _num(now + ttl),
                    ":now": _num(now),
                    ":stale": _num(now - REQUEST_STALE_AFTER),
                    ":iso": {"S": _iso(now)},
                    ":one": {"N": "1"},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        self.epoch = int(resp["Attributes"]["epoch"]["N"])
        self.expires_at = now + ttl
        log.info("Lease acquired by %s (epoch %d) for %.0fs", self.holder, self.epoch, ttl)
        return True

    # Each call below works on the epoch it started with, and only forgets that
    # epoch: a slow call from before a re-acquire must not wipe the new one.

    def renew(self, ttl: float) -> bool:
        epoch = self.epoch
        if epoch is None:
            return False
        now = self.clock()
        try:
            self._update(
                "SET #exp = :exp, renewed_at = :iso",
                # Not after a release: a renewal still in flight mustn't bring the lease back.
                "#epoch = :epoch AND #holder = :holder AND attribute_not_exists(released_at)",
                {":exp": _num(now + ttl), ":iso": {"S": _iso(now)}, **self._mine(epoch)},
            )
        except ClientError as exc:
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise
            log.warning("Lease lost: another runner took over from %s", self.holder)
            self._forget(epoch)
            return False
        if self.epoch == epoch:
            self.expires_at = now + ttl
        return True

    def release(self) -> None:
        epoch = self.epoch
        if epoch is None:
            return
        now = self.clock()
        try:
            self._update(
                "SET #exp = :now, released_at = :iso",
                "#epoch = :epoch AND #holder = :holder",
                {":now": _num(now), ":iso": {"S": _iso(now)}, **self._mine(epoch)},
            )
            log.info("Lease released by %s", self.holder)
        except ClientError as exc:
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise
        finally:
            self._forget(epoch)

    def _forget(self, epoch: int | None) -> None:
        if self.epoch == epoch:
            self.epoch = None

    def read(self) -> LeaseInfo | None:
        item = self.client.get_item(TableName=self.table, Key=_key(LEASE, LEASE), ConsistentRead=True).get("Item")
        if not item:
            return None
        data = _plain(item)
        return LeaseInfo(**{k: data[k] for k in LeaseInfo.__dataclass_fields__ if k in data})

    def verify(self) -> None:
        self.check()
        epoch = self.epoch
        info = self.read()
        if not info or info.epoch != epoch or info.holder != self.holder or info.expires_at <= self.clock():
            self._forget(epoch)
            raise LeaseLost(f"{self.holder} no longer holds the lease (now {info.holder if info else 'nobody'})")

    def request_handoff(self) -> None:
        now = self.clock()
        try:
            self._update(
                "SET requested_by = :holder, requested_at = :now",
                "attribute_exists(pk) AND #holder <> :holder",
                {":holder": {"S": self.holder}, ":now": _num(now)},
            )
        except ClientError as exc:
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise

    def handoff_requested(self) -> str | None:
        info = self.read()
        if info and info.requested_by and info.requested_by != self.holder:
            if info.requested_at and self.clock() - info.requested_at < REQUEST_STALE_AFTER:
                return info.requested_by
        return None

    def condition_check(self, epoch: int) -> dict[str, Any]:
        """A transaction item that fails unless we still hold the lease with this epoch."""
        self.check()
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _key(LEASE, LEASE),
                "ConditionExpression": "#epoch = :epoch AND #holder = :holder AND #exp > :now",
                "ExpressionAttributeNames": dict(self._NAMES),
                "ExpressionAttributeValues": {":now": _num(self.clock()), **self._mine(epoch)},
            }
        }

    def _mine(self, epoch: int | None = None) -> dict[str, Any]:
        return {":epoch": {"N": str(self.epoch if epoch is None else epoch)}, ":holder": {"S": self.holder}}

    def _update(self, expression: str, condition: str, values: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        names = {k: v for k, v in self._NAMES.items() if k in expression or k in condition}
        for attempt in range(10):
            try:
                return self.client.update_item(
                    TableName=self.table,
                    Key=_key(LEASE, LEASE),
                    UpdateExpression=expression,
                    ConditionExpression=condition,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    **kwargs,
                )
            except ClientError as exc:
                # A company save's lease check can briefly conflict with this update.
                if _error_code(exc) != "TransactionConflictException" or attempt == 9:
                    raise
                _backoff(attempt, cap=1.0)
        raise AssertionError("unreachable")


class DynamoStateStore(StateStore):
    def __init__(self, client: Any, table: str, lease: DynamoLease | None = None, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.table = table
        self.lease = lease  # saves are fenced on it when set
        self.clock = clock

    # -- reads (strongly consistent) -----------------------------------------

    def _query(self, pk: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        args = {
            "TableName": self.table,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ConsistentRead": True,
            **kwargs,
        }
        while True:
            page = self.client.query(**args)
            yield from (_plain(item) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page or kwargs.get("Limit"):
                return
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def load_schedule(self) -> dict[str, CompanyMeta]:
        return {item["sk"]: CompanyMeta.from_dict(item) for item in self._query(SCHEDULE)}

    def load_switches(self) -> dict[str, bool]:
        item = self.client.get_item(TableName=self.table, Key=_key(SWITCHES, SWITCHES), ConsistentRead=True).get("Item")
        return {k: bool(v) for k, v in _plain(item).items() if k in SWITCH_NAMES} if item else {}

    def save_switch(self, name: str, on: bool) -> None:
        """Not fenced on the lease: a switch is the user's, and any runner may read it."""
        self.client.update_item(
            TableName=self.table, Key=_key(SWITCHES, SWITCHES), UpdateExpression="SET #r = :on",
            ExpressionAttributeNames={"#r": name}, ExpressionAttributeValues={":on": {"BOOL": on}},
        )

    def load_digest(self) -> DigestSchedule:
        rows = {item["sk"]: item for item in self._query(DIGEST)}
        schedule = DigestSchedule.from_dict(rows.get(DIGEST, {}))
        schedule.requested_at = rows.get(REQUEST, {}).get("requested_at")
        return schedule

    def save_digest(self, schedule: DigestSchedule) -> None:
        attrs = {k: v for k, v in compact(schedule).items() if k != "requested_at"}  # the app's row, not ours
        writes = [{"Put": {"TableName": self.table, "Item": _item(DIGEST, DIGEST, attrs)}}]
        epoch = self.lease.epoch if self.lease else None
        self._transact([self.lease.condition_check(epoch), *writes] if self.lease else writes, epoch)

    def load_company(self, company: str) -> CompanyRecord:
        jobs = {item["sk"]: SeenJob.from_dict(item) for item in self._query(company)}
        meta = self.client.get_item(TableName=self.table, Key=_key(SCHEDULE, company), ConsistentRead=True).get("Item")
        return CompanyRecord(
            company,
            jobs,
            CompanyMeta.from_dict(_plain(meta)) if meta else CompanyMeta(),
            loaded={uid: compact(job) for uid, job in jobs.items()},
        )

    def recent_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        # Eventually consistent: it's a log, and Live Tracking reads it every few seconds.
        alerts = list(self._query(ALERTS, ScanIndexForward=False, Limit=limit, ConsistentRead=False))
        for alert in alerts:
            alert["notified_at"] = alert["sk"].split("#", 1)[0]
        return alerts

    def last_runs(self) -> dict[str, dict[str, Any]]:
        return {item["sk"]: item for item in self._query(RUNS)}

    # -- writes ------------------------------------------------------------

    def save_company(self, record: CompanyRecord) -> None:
        """Write only the rows that changed since load_company(), fenced on the lease.

        Up to 99 rows go in one transaction with the lease check. Bigger saves
        (a large company's first check) are split, with the schedule row in the
        last transaction, so the company only counts as checked once it's all in.
        """
        current = {uid: compact(job) for uid, job in record.jobs.items()}
        loaded = record.loaded or {}
        writes = [
            {"Put": {"TableName": self.table, "Item": _item(record.name, uid, attrs)}}
            for uid, attrs in current.items()
            if loaded.get(uid) != attrs
        ]
        writes += [{"Delete": {"TableName": self.table, "Key": _key(record.name, uid)}} for uid in loaded if uid not in current]
        writes += [self._alert_row(record, uid) for uid in record.alerted if record.jobs.get(uid)]
        writes += self._queue_writes(record, current, loaded)
        writes.append({"Put": {"TableName": self.table, "Item": _item(SCHEDULE, record.name, compact(record.meta))}})

        epoch = self.lease.epoch if self.lease else None
        if self.lease and epoch is None:
            raise LeaseLost(f"{self.lease.holder} doesn't hold the lease; nothing was saved")
        size = MAX_TRANSACTION - 1 if self.lease else MAX_TRANSACTION
        for start in range(0, len(writes), size):
            chunk = writes[start : start + size]
            self._transact([self.lease.condition_check(epoch), *chunk] if self.lease else chunk, epoch)
        record.loaded = current
        record.alerted = []

    def save_meta(self, company: str, meta: CompanyMeta) -> None:
        writes = [{"Put": {"TableName": self.table, "Item": _item(SCHEDULE, company, compact(meta))}}]
        epoch = self.lease.epoch if self.lease else None
        self._transact([self.lease.condition_check(epoch), *writes] if self.lease else writes, epoch)

    def _alert_row(self, record: CompanyRecord, uid: str) -> dict[str, Any]:
        job = record.jobs[uid]
        attrs = {
            "company": record.name,
            "title": job.title,
            "location": job.location,
            "url": job.url,
            "first_seen": job.first_seen,
            "by": self.lease.holder if self.lease else None,
            "ttl": int(self.clock()) + ALERT_TTL,
        }
        sk = f"{job.notified_at}#{record.name}#{uid}"
        return {"Put": {"TableName": self.table, "Item": _item(ALERTS, sk, {k: v for k, v in attrs.items() if v})}}

    def _queue_writes(self, record: CompanyRecord, current: dict[str, dict], loaded: dict[str, dict]) -> list[dict[str, Any]]:
        """Keep the company's #queue rows in step with its job rows."""
        writes = []
        for uid in current.keys() | loaded.keys():
            before, after = loaded.get(uid), current.get(uid)
            if before == after:
                continue
            job = record.jobs.get(uid)
            if job and _waiting(after):
                writes.append({"Update": self._queue_update(record.name, uid, job)})
            elif job and job.dropped_for == SKIPPED and _waiting(before):
                writes.append({"Update": self._queue_update(record.name, uid, job, done=True)})
            elif _waiting(before):
                writes.append({"Delete": {"TableName": self.table, "Key": _key(QUEUE, f"{record.name}#{uid}")}})
        return writes

    def _queue_update(self, company: str, uid: str, job: SeenJob, done: bool = False) -> dict[str, Any]:
        """Set a #queue row's fields, keeping when it joined the list and the user's skip."""
        now = self.clock()
        fields: dict[str, Any] = {"company": company, "uid": uid, "title": job.title, "url": job.url,
                                  "first_seen": job.first_seen, "location": job.location}
        if done:
            fields |= {"done_at": _iso(now), "ttl": int(now + SKIPPED_KEPT.total_seconds())}
        names = {f"#f{i}": name for i, name in enumerate(fields)} | {"#q": "queued_at"}
        values = {f":f{i}": _serialize(v) for i, v in enumerate(fields.values()) if v is not None} | {":now": {"S": _iso(now)}}
        sets = [f"#f{i} = :f{i}" for i, v in enumerate(fields.values()) if v is not None] + ["#q = if_not_exists(#q, :now)"]
        removes = [f"#f{i}" for i, v in enumerate(fields.values()) if v is None]
        return {
            "TableName": self.table, "Key": _key(QUEUE, f"{company}#{uid}"),
            "UpdateExpression": "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else ""),
            "ExpressionAttributeNames": names, "ExpressionAttributeValues": values,
        }

    def _transact(self, items: list[dict[str, Any]], epoch: int | None = None) -> None:
        # boto3 doesn't retry cancelled transactions, so throttling (common on a
        # small provisioned table during a big first load) is retried here, ~20 s in all.
        for attempt in range(TRANSACTION_ATTEMPTS):
            try:
                self.client.transact_write_items(TransactItems=items)
                return
            except ClientError as exc:
                if _error_code(exc) != "TransactionCanceledException":
                    raise
                reasons = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
                if self.lease and reasons and reasons[0] == "ConditionalCheckFailed":
                    self.lease._forget(epoch)
                    raise LeaseLost(f"{self.lease.holder} lost the lease; nothing was saved") from exc
                if not _RETRYABLE_TXN.intersection(reasons) or attempt == TRANSACTION_ATTEMPTS - 1:
                    raise
                _backoff(attempt, cap=5.0)

    def record_stats(self, runner: str, hour: str, counts: dict[str, int]) -> None:
        """Add to the hour's row with one update (not fenced: it's informational)."""
        counts = {name: value for name, value in counts.items() if value}
        if not counts:
            return
        names = {f"#c{i}": name for i, name in enumerate(counts)}
        values = {f":c{i}": {"N": str(value)} for i, value in enumerate(counts.values())}
        self.client.update_item(
            TableName=self.table, Key=_key(STATS, f"{hour}#{runner}"),
            UpdateExpression="SET #ttl = :ttl ADD " + ", ".join(f"#c{i} :c{i}" for i in range(len(counts))),
            ExpressionAttributeNames={**names, "#ttl": "ttl"},
            ExpressionAttributeValues={**values, ":ttl": {"N": str(int(self.clock() + STATS_KEPT.total_seconds()))}},
        )

    def load_stats(self, since: str) -> list[dict[str, Any]]:
        rows = self._query(
            STATS, KeyConditionExpression="pk = :pk AND sk >= :since", ConsistentRead=False,
            ExpressionAttributeValues={":pk": {"S": STATS}, ":since": {"S": since}},
        )
        out = []
        for row in rows:
            hour, runner = row.pop("sk").split("#", 1)
            counts = {k: v for k, v in row.items() if k not in ("pk", "ttl")}
            out.append({"hour": hour, "runner": runner, **counts})
        return out

    def record_run(self, runner: str, summary: dict[str, Any]) -> None:
        """Remember a runner's last pass for `status` (not fenced: it's informational)."""
        self.client.put_item(TableName=self.table, Item=_item(RUNS, runner, {k: v for k, v in summary.items() if v is not None}))

    def record_round(self, runner: str, progress: dict[str, Any]) -> None:
        """Not fenced: it's informational."""
        attrs = {k: v for k, v in progress.items() if v is not None} | {"ttl": int(self.clock()) + ROUND_TTL}
        self.client.put_item(TableName=self.table, Item=_item(ROUND, runner, attrs))

    def load_rounds(self) -> dict[str, dict[str, Any]]:
        return {item.pop("sk"): {k: v for k, v in item.items() if k not in ("pk", "ttl")}
                for item in self._query(ROUND, ConsistentRead=False)}

    # -- Live Tracking -----------------------------------------------------

    def load_queue(self) -> list[QueuedMatch]:
        now = int(self.clock())
        return [QueuedMatch.from_dict(item) for item in self._query(QUEUE) if item.get("ttl", now) >= now]

    def mark_skipped(self, company: str, uid: str, skipped: bool) -> bool:
        """Not fenced on the lease: a skip is the user's. Only a match still waiting can be (un)skipped."""
        change = {"UpdateExpression": "SET #s = :now", "ExpressionAttributeValues": {":now": {"S": _iso(self.clock())}}} \
            if skipped else {"UpdateExpression": "REMOVE #s"}
        try:
            self.client.update_item(
                TableName=self.table, Key=_key(QUEUE, f"{company}#{uid}"), **change,
                ConditionExpression="attribute_exists(pk) AND attribute_not_exists(#d)",
                ExpressionAttributeNames={"#s": "skipped_at", "#d": "done_at"},
            )
        except ClientError as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def request_digest(self) -> None:
        self.client.put_item(TableName=self.table, Item=_item(DIGEST, REQUEST, {"requested_at": _iso(self.clock())}))

    def repair_queue(self, add: list[QueuedMatch], remove: list[tuple[str, str]]) -> None:
        """Rows a save would have written but didn't (matches from before #queue existed), and rows left behind."""
        for match in add:
            with contextlib.suppress(ClientError):  # it appeared meanwhile: the save's row is the better one
                self.client.put_item(
                    TableName=self.table, ConditionExpression="attribute_not_exists(pk)",
                    Item=_item(QUEUE, f"{match.company}#{match.uid}", {k: v for k, v in asdict(match).items() if v is not None}
                               | {"queued_at": _iso(self.clock())}),
                )
        for company, uid in remove:
            self.client.delete_item(TableName=self.table, Key=_key(QUEUE, f"{company}#{uid}"))

    # -- whole state (migration) -------------------------------------------

    def load(self) -> MonitorState:
        state = MonitorState()
        args: dict[str, Any] = {"TableName": self.table, "ConsistentRead": True}
        while True:
            page = self.client.scan(**args)
            for item in map(_plain, page.get("Items", [])):
                if item["pk"] == SCHEDULE:
                    state.meta[item["sk"]] = CompanyMeta.from_dict(item)
                elif item["pk"] == DIGEST and item["sk"] == DIGEST:
                    requested = state.digest.requested_at
                    state.digest = DigestSchedule.from_dict(item)
                    state.digest.requested_at = requested
                elif item["pk"] == DIGEST and item["sk"] == REQUEST:
                    state.digest.requested_at = item.get("requested_at")
                elif item["pk"] == SWITCHES:
                    state.switches = {k: bool(v) for k, v in item.items() if k in SWITCH_NAMES}
                elif not item["pk"].startswith("#"):
                    state.jobs_for(item["pk"])[item["sk"]] = SeenJob.from_dict(item)
            if "LastEvaluatedKey" not in page:
                return state
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def save(self, state: MonitorState) -> None:
        """Write every company in `state`, one company at a time, fenced on the lease like any save.

        Rows not in `state` are left alone. Meant for migrating into a new
        table; a migration that stops part-way can simply be run again.
        """
        for name in sorted(set(state.companies) | set(state.meta)):
            record = CompanyRecord(name, state.companies.get(name, {}), state.meta.get(name, CompanyMeta()), loaded={})
            self.save_company(record)
        if state.digest.next_send_at:
            self.save_digest(state.digest)
