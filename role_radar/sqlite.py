"""SQLite state store: everything on this Mac, in one file (runtime.storage: sqlite).

For running Role Radar without AWS. It keeps the same rows as the DynamoDB
table (dynamo.py), as one table of (pk, sk) → JSON, so the checker and the
commands the menu bar app runs (switches, skips, "Send now") can all write at
once: each write changes only its own rows, in a transaction. There's one
machine, so no lease to fence saves on: `role-radar start` runs once per Mac
(instance.py), and `run --once` refuses to run beside it.

  pk            sk                         data
  <company>     <job uid>                  a seen job: the fields of storage.SeenJob
  #schedule     <company>                  its schedule: storage.CompanyMeta
  #digest       #digest / #request         the digest's schedule / the latest "Send now"
  #switches     #switches                  runner and alert channel switches
  #alerts       <time>#<company>#<uid>     log of sent alerts; expire after 30 days
  #runs         <runner>                   each runner's last pass
  #stats        <hour>#<runner>            a runner's activity counts for one UTC hour; expire after 2 days
  #queue        <company>#<uid>            a match waiting for the digest, for Live Tracking
  #round        <runner>                   how far the runner's pass in progress has got
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from role_radar.storage import (
    SKIPPED_KEPT, STATS_KEPT, SWITCHES as SWITCH_NAMES, CompanyMeta, CompanyRecord, DigestSchedule, MonitorState,
    QueuedMatch, SeenJob, StateStore, compact, queue_changes, to_iso,
)

SCHEDULE, DIGEST, REQUEST, SWITCHES = "#schedule", "#digest", "#request", "#switches"
ALERTS, RUNS, STATS, QUEUE, ROUND = "#alerts", "#runs", "#stats", "#queue", "#round"
ALERT_TTL = 30 * 86400
ROUND_TTL = 2 * 86400
SWEEP_EVERY = 3600.0  # seconds between deleting expired rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    pk TEXT NOT NULL,
    sk TEXT NOT NULL,
    data TEXT NOT NULL,
    ttl INTEGER,
    PRIMARY KEY (pk, sk)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS items_ttl ON items (ttl) WHERE ttl IS NOT NULL;
"""


class SqliteStateStore(StateStore):
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()  # one connection, shared by the runner's threads
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")  # readers never wait for the writer
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._swept_at = 0.0

    def close(self) -> None:
        self._db.close()

    # -- rows ------------------------------------------------------------------

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: taken at once, so two processes' changes to the same row don't interleave."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _rows(self, pk: str, *, since: str | None = None, newest_first: bool = False, limit: int | None = None
              ) -> list[tuple[str, dict[str, Any]]]:
        sql = "SELECT sk, data FROM items WHERE pk = ? AND (ttl IS NULL OR ttl >= ?)"
        params: list[Any] = [pk, int(self.clock())]
        if since is not None:
            sql += " AND sk >= ?"
            params.append(since)
        sql += " ORDER BY sk" + (" DESC" if newest_first else "")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return [(sk, json.loads(data)) for sk, data in self._db.execute(sql, params)]

    def _get(self, pk: str, sk: str, db: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        with self._lock:
            row = (db or self._db).execute(
                "SELECT data FROM items WHERE pk = ? AND sk = ? AND (ttl IS NULL OR ttl >= ?)", (pk, sk, int(self.clock()))
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db: sqlite3.Connection, pk: str, sk: str, data: dict[str, Any], ttl: int | None = None) -> None:
        db.execute("INSERT OR REPLACE INTO items (pk, sk, data, ttl) VALUES (?, ?, ?, ?)",
                   (pk, sk, json.dumps(data, separators=(",", ":")), ttl))

    @staticmethod
    def _delete(db: sqlite3.Connection, pk: str, sk: str) -> None:
        db.execute("DELETE FROM items WHERE pk = ? AND sk = ?", (pk, sk))

    def _now(self) -> str:
        return to_iso(datetime.fromtimestamp(self.clock(), timezone.utc).replace(microsecond=0))

    def _sweep(self, db: sqlite3.Connection) -> None:
        """Delete expired rows (DynamoDB's TTL does this by itself), at most every SWEEP_EVERY seconds."""
        now = self.clock()
        if now - self._swept_at >= SWEEP_EVERY:
            db.execute("DELETE FROM items WHERE ttl IS NOT NULL AND ttl < ?", (int(now),))
            self._swept_at = now

    # -- the schedule and one company's jobs ------------------------------------

    def load_schedule(self) -> dict[str, CompanyMeta]:
        return {sk: CompanyMeta.from_dict(data) for sk, data in self._rows(SCHEDULE)}

    def load_company(self, company: str) -> CompanyRecord:
        jobs = {uid: SeenJob.from_dict(data) for uid, data in self._rows(company)}
        meta = self._get(SCHEDULE, company)
        return CompanyRecord(company, jobs, CompanyMeta.from_dict(meta) if meta else CompanyMeta(),
                             loaded={uid: compact(job) for uid, job in jobs.items()})

    def save_company(self, record: CompanyRecord) -> None:
        """Write the rows that changed since load_company(), its alert log rows and list rows, and its schedule, at once."""
        current = {uid: compact(job) for uid, job in record.jobs.items()}
        loaded = record.loaded or {}
        with self._tx() as db:
            for uid, attrs in current.items():
                if loaded.get(uid) != attrs:
                    self._put(db, record.name, uid, attrs)
            for uid in loaded.keys() - current.keys():
                self._delete(db, record.name, uid)
            for uid in record.alerted:
                if record.jobs.get(uid):
                    self._log_alert(db, record, uid)
            for change, uid, job in queue_changes(record, current, loaded):
                if job:
                    self._queue_put(db, record.name, uid, job, done=change == "done")
                else:
                    self._delete(db, QUEUE, f"{record.name}#{uid}")
            self._put(db, SCHEDULE, record.name, compact(record.meta))
            self._sweep(db)
        record.loaded = current
        record.alerted = []

    def save_meta(self, company: str, meta: CompanyMeta) -> None:
        with self._tx() as db:
            self._put(db, SCHEDULE, company, compact(meta))

    def _log_alert(self, db: sqlite3.Connection, record: CompanyRecord, uid: str) -> None:
        job = record.jobs[uid]
        attrs = {"company": record.name, "title": job.title, "location": job.location, "url": job.url,
                 "first_seen": job.first_seen, "by": "laptop"}
        self._put(db, ALERTS, f"{job.notified_at}#{record.name}#{uid}", {k: v for k, v in attrs.items() if v},
                  int(self.clock()) + ALERT_TTL)

    def _queue_put(self, db: sqlite3.Connection, company: str, uid: str, job: SeenJob, done: bool = False) -> None:
        """Set a list row's fields, keeping when it joined the list and the user's skip."""
        key = f"{company}#{uid}"
        row = self._get(QUEUE, key, db) or {}
        row.update(company=company, uid=uid, title=job.title, url=job.url, first_seen=job.first_seen,
                   location=job.location, queued_at=row.get("queued_at") or self._now())
        ttl = None
        if done:
            row["done_at"] = self._now()
            ttl = int(self.clock() + SKIPPED_KEPT.total_seconds())
        self._put(db, QUEUE, key, {k: v for k, v in row.items() if v is not None}, ttl)

    # -- switches and the digest ------------------------------------------------

    def load_switches(self) -> dict[str, bool]:
        data = self._get(SWITCHES, SWITCHES) or {}
        return {k: bool(v) for k, v in data.items() if k in SWITCH_NAMES}

    def save_switch(self, name: str, on: bool) -> None:
        with self._tx() as db:
            data = self._get(SWITCHES, SWITCHES, db) or {}
            data[name] = on
            self._put(db, SWITCHES, SWITCHES, data)

    def load_digest(self) -> DigestSchedule:
        rows = dict(self._rows(DIGEST))
        schedule = DigestSchedule.from_dict(rows.get(DIGEST, {}))
        schedule.requested_at = rows.get(REQUEST, {}).get("requested_at")
        return schedule

    def save_digest(self, schedule: DigestSchedule) -> None:
        with self._tx() as db:
            self._put(db, DIGEST, DIGEST, {k: v for k, v in compact(schedule).items() if k != "requested_at"})

    def request_digest(self) -> None:
        with self._tx() as db:
            self._put(db, DIGEST, REQUEST, {"requested_at": self._now()})

    # -- Live Tracking ------------------------------------------------------------

    def load_queue(self) -> list[QueuedMatch]:
        return [QueuedMatch.from_dict(data) for _, data in self._rows(QUEUE)]

    def mark_skipped(self, company: str, uid: str, skipped: bool) -> bool:
        with self._tx() as db:
            row = self._get(QUEUE, f"{company}#{uid}", db)
            if not row or row.get("done_at"):
                return False
            if skipped:
                row["skipped_at"] = self._now()
            else:
                row.pop("skipped_at", None)
            self._put(db, QUEUE, f"{company}#{uid}", row)
        return True

    def mark_send(self, company: str, uid: str) -> bool:
        with self._tx() as db:
            row = self._get(QUEUE, f"{company}#{uid}", db)
            if not row or row.get("done_at") or row.get("skipped_at"):
                return False
            row["send_at"] = self._now()
            self._put(db, QUEUE, f"{company}#{uid}", row)
        return True

    def repair_queue(self, add: list[QueuedMatch], remove: list[tuple[str, str]]) -> None:
        with self._tx() as db:
            for match in add:
                key = f"{match.company}#{match.uid}"
                if self._get(QUEUE, key, db) is None:
                    attrs = {k: v for k, v in asdict(match).items() if v is not None}
                    self._put(db, QUEUE, key, attrs | {"queued_at": self._now()})
            for company, uid in remove:
                self._delete(db, QUEUE, f"{company}#{uid}")

    # -- for `status` and the menu bar app --------------------------------------

    def recent_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        return [{**data, "notified_at": sk.split("#", 1)[0]} for sk, data in self._rows(ALERTS, newest_first=True, limit=limit)]

    def record_run(self, runner: str, summary: dict[str, Any]) -> None:
        with self._tx() as db:
            self._put(db, RUNS, runner, {k: v for k, v in summary.items() if v is not None})

    def last_runs(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows(RUNS))

    def record_stats(self, runner: str, hour: str, counts: dict[str, int]) -> None:
        counts = {name: value for name, value in counts.items() if value}
        if not counts:
            return
        with self._tx() as db:
            row = self._get(STATS, f"{hour}#{runner}", db) or {}
            for name, value in counts.items():
                row[name] = row.get(name, 0) + value
            self._put(db, STATS, f"{hour}#{runner}", row, int(self.clock() + STATS_KEPT.total_seconds()))

    def load_stats(self, since: str) -> list[dict[str, Any]]:
        out = []
        for sk, counts in self._rows(STATS, since=since):
            hour, runner = sk.split("#", 1)
            out.append({"hour": hour, "runner": runner, **counts})
        return out

    def record_round(self, runner: str, progress: dict[str, Any]) -> None:
        with self._tx() as db:
            self._put(db, ROUND, runner, {k: v for k, v in progress.items() if v is not None}, int(self.clock()) + ROUND_TTL)

    def load_rounds(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows(ROUND))

    # -- whole state (migration) ----------------------------------------------------

    def load(self) -> MonitorState:
        state = MonitorState()
        with self._lock:
            rows = list(self._db.execute("SELECT pk, sk, data FROM items"))
        for pk, sk, data in rows:
            item = json.loads(data)
            if pk == SCHEDULE:
                state.meta[sk] = CompanyMeta.from_dict(item)
            elif pk == DIGEST and sk == DIGEST:
                requested = state.digest.requested_at
                state.digest = DigestSchedule.from_dict(item)
                state.digest.requested_at = requested
            elif pk == DIGEST and sk == REQUEST:
                state.digest.requested_at = item.get("requested_at")
            elif pk == SWITCHES:
                state.switches = {k: bool(v) for k, v in item.items() if k in SWITCH_NAMES}
            elif not pk.startswith("#"):
                state.jobs_for(pk)[sk] = SeenJob.from_dict(item)
        return state

    def save(self, state: MonitorState) -> None:
        """Write every company in `state`; rows not in it are left alone (for `role-radar migrate`)."""
        for name in sorted(set(state.companies) | set(state.meta)):
            self.save_company(CompanyRecord(name, state.companies.get(name, {}), state.meta.get(name, CompanyMeta()), loaded={}))
        if state.digest.next_send_at:
            self.save_digest(state.digest)
        for name, on in state.switches.items():
            self.save_switch(name, on)
