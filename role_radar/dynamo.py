"""DynamoDB state store and lease, sharing one table (single-table design).

  pk            sk                         item
  <company>     <job uid>                  a seen job: the fields of storage.SeenJob
  #schedule     <company>                  its schedule: storage.CompanyMeta
  #lease        #lease                     who may check companies right now (lease.py)
  #alerts       <time>#<company>#<uid>     log of sent alerts for `status`; expire via TTL
  #runs         <runner>                   each runner's last pass, for `status`

Company names can't start with "#" (the config loader rejects them), so they
never collide with the rows the store keeps for itself.

Reads are strongly consistent: straight after a handoff, the new runner must
see everything the previous one wrote. Saving a company is a transaction that
also checks the lease, so the save commits only while this runner still holds
the lease with the epoch it acquired.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from role_radar.lease import Lease, LeaseInfo, LeaseLost
from role_radar.storage import CompanyMeta, CompanyRecord, MonitorState, SeenJob, StateStore, compact, to_iso

log = logging.getLogger(__name__)

SCHEDULE = "#schedule"
LEASE = "#lease"
ALERTS = "#alerts"
RUNS = "#runs"
ALERT_TTL = 30 * 86400  # seconds an alert-log row lives (the table's TTL attribute is "ttl")
MAX_TRANSACTION = 100  # DynamoDB's limit on actions per TransactWriteItems
REQUEST_STALE_AFTER = 120  # seconds a handoff request keeps others from taking the lease
_RETRYABLE_TXN = {"ThrottlingError", "ProvisionedThroughputExceeded", "TransactionConflict"}

_serialize = TypeSerializer().serialize
_deserialize = TypeDeserializer().deserialize


def _key(pk: str, sk: str) -> dict[str, Any]:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _item(pk: str, sk: str, attrs: dict[str, Any]) -> dict[str, Any]:
    return {**_key(pk, sk), **{k: _serialize(v) for k, v in attrs.items()}}


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, value in item.items():
        value = _deserialize(value)
        if isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        out[name] = value
    return out


def _num(value: float) -> dict[str, str]:
    return {"N": repr(round(value, 3))}


def _iso(ts: float) -> str:
    return to_iso(datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0))


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _backoff(attempt: int) -> None:
    time.sleep(min(0.05 * 2**attempt, 2.0))


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

    def renew(self, ttl: float) -> bool:
        if self.epoch is None:
            return False
        now = self.clock()
        try:
            self._update(
                "SET #exp = :exp, renewed_at = :iso",
                "#epoch = :epoch AND #holder = :holder",
                {":exp": _num(now + ttl), ":iso": {"S": _iso(now)}, **self._mine()},
            )
        except ClientError as exc:
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise
            log.warning("Lease lost: another runner took over from %s", self.holder)
            self.epoch = None
            return False
        self.expires_at = now + ttl
        return True

    def release(self) -> None:
        if self.epoch is None:
            return
        now = self.clock()
        try:
            self._update(
                "SET #exp = :now, released_at = :iso",
                "#epoch = :epoch AND #holder = :holder",
                {":now": _num(now), ":iso": {"S": _iso(now)}, **self._mine()},
            )
            log.info("Lease released by %s", self.holder)
        except ClientError as exc:
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise
        finally:
            self.epoch = None

    def read(self) -> LeaseInfo | None:
        item = self.client.get_item(TableName=self.table, Key=_key(LEASE, LEASE), ConsistentRead=True).get("Item")
        if not item:
            return None
        data = _plain(item)
        return LeaseInfo(**{k: data[k] for k in LeaseInfo.__dataclass_fields__ if k in data})

    def verify(self) -> None:
        self.check()
        info = self.read()
        if not info or info.epoch != self.epoch or info.holder != self.holder or info.expires_at <= self.clock():
            self.epoch = None
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

    def condition_check(self) -> dict[str, Any]:
        """A transaction item that fails unless we still hold the lease with the same epoch."""
        self.check()
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _key(LEASE, LEASE),
                "ConditionExpression": "#epoch = :epoch AND #holder = :holder AND #exp > :now",
                "ExpressionAttributeNames": dict(self._NAMES),
                "ExpressionAttributeValues": {":now": _num(self.clock()), **self._mine()},
            }
        }

    def _mine(self) -> dict[str, Any]:
        return {":epoch": {"N": str(self.epoch)}, ":holder": {"S": self.holder}}

    def _update(self, expression: str, condition: str, values: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        names = {k: v for k, v in self._NAMES.items() if k in expression or k in condition}
        for attempt in range(5):
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
                if _error_code(exc) != "TransactionConflictException" or attempt == 4:
                    raise
                _backoff(attempt)
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
        return list(self._query(ALERTS, ScanIndexForward=False, Limit=limit))

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
        writes.append({"Put": {"TableName": self.table, "Item": _item(SCHEDULE, record.name, compact(record.meta))}})

        size = MAX_TRANSACTION - 1 if self.lease else MAX_TRANSACTION
        for start in range(0, len(writes), size):
            chunk = writes[start : start + size]
            self._transact([self.lease.condition_check(), *chunk] if self.lease else chunk)
        record.loaded = current
        record.alerted = []

    def _alert_row(self, record: CompanyRecord, uid: str) -> dict[str, Any]:
        job = record.jobs[uid]
        attrs = {
            "company": record.name,
            "title": job.title,
            "location": job.location,
            "url": job.url,
            "by": self.lease.holder if self.lease else None,
            "ttl": int(self.clock()) + ALERT_TTL,
        }
        sk = f"{job.notified_at}#{record.name}#{uid}"
        return {"Put": {"TableName": self.table, "Item": _item(ALERTS, sk, {k: v for k, v in attrs.items() if v})}}

    def _transact(self, items: list[dict[str, Any]]) -> None:
        for attempt in range(8):
            try:
                self.client.transact_write_items(TransactItems=items)
                return
            except ClientError as exc:
                if _error_code(exc) != "TransactionCanceledException":
                    raise
                reasons = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
                if self.lease and reasons and reasons[0] == "ConditionalCheckFailed":
                    self.lease.epoch = None
                    raise LeaseLost(f"{self.lease.holder} lost the lease; nothing was saved") from exc
                if not _RETRYABLE_TXN.intersection(reasons) or attempt == 7:
                    raise
                _backoff(attempt)

    def record_run(self, runner: str, summary: dict[str, Any]) -> None:
        """Remember a runner's last pass for `status` (not fenced: it's informational)."""
        self.client.put_item(TableName=self.table, Item=_item(RUNS, runner, {k: v for k, v in summary.items() if v is not None}))

    # -- whole state (migration) -------------------------------------------

    def load(self) -> MonitorState:
        state = MonitorState()
        args: dict[str, Any] = {"TableName": self.table, "ConsistentRead": True}
        while True:
            page = self.client.scan(**args)
            for item in map(_plain, page.get("Items", [])):
                if item["pk"] == SCHEDULE:
                    state.meta[item["sk"]] = CompanyMeta.from_dict(item)
                elif not item["pk"].startswith("#"):
                    state.jobs_for(item["pk"])[item["sk"]] = SeenJob.from_dict(item)
            if "LastEvaluatedKey" not in page:
                return state
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def save(self, state: MonitorState) -> None:
        """Write every job and schedule row in `state`; rows not in it are left alone.

        Meant for migrating into a new table: batch writes, fenced only by
        verifying the lease first.
        """
        if self.lease:
            self.lease.verify()
        items = [_item(company, uid, compact(job)) for company, jobs in state.companies.items() for uid, job in jobs.items()]
        items += [_item(SCHEDULE, company, compact(meta)) for company, meta in state.meta.items() if compact(meta)]
        for start in range(0, len(items), 25):
            pending = [{"PutRequest": {"Item": item}} for item in items[start : start + 25]]
            for attempt in range(10):
                resp = self.client.batch_write_item(RequestItems={self.table: pending})
                pending = resp.get("UnprocessedItems", {}).get(self.table, [])
                if not pending:
                    break
                _backoff(attempt)
            else:
                raise RuntimeError(f"DynamoDB kept {len(pending)} item(s) unprocessed; try again later")
