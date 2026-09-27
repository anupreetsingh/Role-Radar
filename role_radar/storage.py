"""Persistence for seen jobs and each company's check schedule.

Runs work one company at a time: `load_schedule()` to find the companies that
are due, then `load_company()` / `save_company()` around each company's check,
so progress is saved as each company finishes. `load()` / `save()` move a whole
state at once (migration, tests). Swapping JSON for DynamoDB means writing one
new `StateStore` subclass.

The JSON file is written deterministically (sorted keys) and only rewritten
when its content changes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4  # 2: schedules; 3: channel receipts; 4: digest cadence. Older files still load.
ALERT_LOG_SIZE = 50  # alerts the JSON store remembers for `status`


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


RUNNERS = ("laptop", "lambda")  # the runners a switch can turn off


def runner_on(switches: dict[str, bool], runner: str) -> bool:
    return switches.get(runner, True)


def compact(obj: object) -> dict:
    """asdict() without empty values, so stored records stay small."""
    return {k: v for k, v in asdict(obj).items() if v not in (None, False, 0)}


@dataclass
class SeenJob:
    title: str
    url: str
    fingerprint: str
    first_seen: str
    location: str | None = None
    matched: bool = False
    notified_at: str | None = None
    removed_at: str | None = None
    detail_fetched: bool = False
    duplicate_of: str | None = None  # uid of an earlier posting this one repeats
    # Successful channels survive partial failures and runner handoffs. notified_at
    # remains empty until all configured channels have accepted the job.
    notified_channels: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.removed_at is None

    @classmethod
    def from_dict(cls, data: dict) -> SeenJob:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class CompanyMeta:
    """When a company was last checked and when it's next due."""

    last_checked_at: str | None = None
    next_check_at: str | None = None
    failures: int = 0  # consecutive failed checks
    last_error: str | None = None
    last_ok_at: str | None = None  # the last check that succeeded

    @classmethod
    def from_dict(cls, data: dict) -> CompanyMeta:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class DigestSchedule:
    next_send_at: str | None = None
    last_attempt_at: str | None = None
    interval_minutes: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> DigestSchedule:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CompanyRecord:
    """One company's stored state, loaded for a check and saved after it."""

    name: str
    jobs: dict[str, SeenJob] = field(default_factory=dict)
    meta: CompanyMeta = field(default_factory=CompanyMeta)
    # Not persisted as such. `loaded` is what a store read (uid → stored fields),
    # so it can write only the rows that changed; `alerted` lists the uids
    # alerted during this check, for the store's alert log.
    loaded: dict[str, dict] | None = field(default=None, repr=False, compare=False)
    alerted: list[str] = field(default_factory=list, repr=False, compare=False)
    delivery_changed: bool = field(default=False, repr=False, compare=False)

    @property
    def is_new(self) -> bool:
        """Never checked successfully, and nothing recorded (a migrated company has jobs but no schedule).

        A first check that failed doesn't count, so `notify_on_first_run: false`
        still applies to the first check that works.
        """
        checked_ok = self.meta.last_ok_at or (self.meta.last_checked_at and not self.meta.failures)
        return not checked_ok and not self.jobs


@dataclass
class MonitorState:
    # company name → job uid → record
    companies: dict[str, dict[str, SeenJob]] = field(default_factory=dict)
    # company name → schedule
    meta: dict[str, CompanyMeta] = field(default_factory=dict)
    # For `status` with JSON storage: each runner's last pass, and the latest alerts.
    runs: dict[str, dict] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    digest: DigestSchedule = field(default_factory=DigestSchedule)
    # Runner on/off switches ("laptop", "lambda"); a runner not listed is on.
    switches: dict[str, bool] = field(default_factory=dict)

    def jobs_for(self, company: str) -> dict[str, SeenJob]:
        return self.companies.setdefault(company, {})

    def is_new_company(self, company: str) -> bool:
        return not self.companies.get(company)

    def prune(self, retention_days: int, now: datetime | None = None) -> int:
        """Forget jobs removed more than `retention_days` ago. Returns count pruned."""
        cutoff = (now or utcnow()) - timedelta(days=retention_days)
        pruned = 0
        for jobs in self.companies.values():
            for uid in [u for u, j in jobs.items() if j.removed_at and from_iso(j.removed_at) < cutoff]:
                del jobs[uid]
                pruned += 1
        return pruned

    def record(self, company: str) -> CompanyRecord:
        """A copy of one company's state."""
        jobs = {uid: SeenJob.from_dict(asdict(j)) for uid, j in self.companies.get(company, {}).items()}
        meta = CompanyMeta.from_dict(asdict(self.meta[company])) if company in self.meta else CompanyMeta()
        return CompanyRecord(company, jobs, meta)

    def put(self, record: CompanyRecord) -> None:
        """Replace one company's state with a copy of `record`."""
        self.companies[record.name] = {uid: SeenJob.from_dict(asdict(j)) for uid, j in record.jobs.items()}
        self.meta[record.name] = CompanyMeta.from_dict(asdict(record.meta))

    def to_dict(self) -> dict:
        data = {
            "version": SCHEMA_VERSION,
            "companies": {
                company: {uid: compact(job) for uid, job in jobs.items()} for company, jobs in self.companies.items() if jobs
            },
            "schedule": {company: compact(meta) for company, meta in self.meta.items() if compact(meta)},
        }
        if self.runs:
            data["runs"] = self.runs
        if self.alerts:
            data["alerts"] = self.alerts
        if self.digest.next_send_at:
            data["digest"] = compact(self.digest)
        if self.switches:
            data["switches"] = self.switches
        return data

    @classmethod
    def from_dict(cls, data: dict) -> MonitorState:
        companies = {
            company: {uid: SeenJob.from_dict(job) for uid, job in jobs.items()}
            for company, jobs in (data.get("companies") or {}).items()
        }
        meta = {company: CompanyMeta.from_dict(m) for company, m in (data.get("schedule") or {}).items()}
        return cls(companies=companies, meta=meta, runs=dict(data.get("runs") or {}), alerts=list(data.get("alerts") or []),
                   digest=DigestSchedule.from_dict(data.get("digest") or {}),
                   switches={k: bool(v) for k, v in (data.get("switches") or {}).items()})


class StateStore(ABC):
    def load_switches(self) -> dict[str, bool]:
        """Runner on/off switches; a runner not listed is on."""
        return {}

    def save_switch(self, runner: str, on: bool) -> None:
        raise NotImplementedError("This store does not support runner switches")

    def load_digest(self) -> DigestSchedule:
        raise NotImplementedError("This store does not support digest scheduling")

    def save_digest(self, schedule: DigestSchedule) -> None:
        raise NotImplementedError("This store does not support digest scheduling")

    @abstractmethod
    def load_schedule(self) -> dict[str, CompanyMeta]:
        """Every company's schedule, to decide which are due."""

    @abstractmethod
    def load_company(self, company: str) -> CompanyRecord: ...

    @abstractmethod
    def save_company(self, record: CompanyRecord) -> None: ...

    @abstractmethod
    def load(self) -> MonitorState: ...

    @abstractmethod
    def save(self, state: MonitorState) -> None: ...

    # -- for `role-radar status` ----------------------------------------------

    def record_run(self, runner: str, summary: dict) -> None:
        """Remember a runner's last pass. Default: not kept."""

    def last_runs(self) -> dict[str, dict]:
        """runner → summary of its last pass."""
        return {}

    def recent_alerts(self, limit: int = 10) -> list[dict]:
        """The latest alerts sent, newest first: company, title, location, url, notified_at, by."""
        return []


class MemoryStateStore(StateStore):
    """Keeps the whole state in memory: for tests and dry runs. Thread-safe."""

    def __init__(self, state: MonitorState | None = None) -> None:
        self.state = state or MonitorState()
        self.saves = 0
        self._lock = threading.RLock()

    def _current(self) -> MonitorState:
        return self.state

    def _persist(self) -> None:
        self.saves += 1

    def load_schedule(self) -> dict[str, CompanyMeta]:
        with self._lock:
            return {name: CompanyMeta.from_dict(asdict(m)) for name, m in self._current().meta.items()}

    def load_switches(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._current().switches)

    def save_switch(self, runner: str, on: bool) -> None:
        with self._lock:
            self._current().switches[runner] = on
            self._persist()

    def load_digest(self) -> DigestSchedule:
        with self._lock:
            return DigestSchedule.from_dict(asdict(self._current().digest))

    def save_digest(self, schedule: DigestSchedule) -> None:
        with self._lock:
            self._current().digest = DigestSchedule.from_dict(asdict(schedule))
            self._persist()

    def load_company(self, company: str) -> CompanyRecord:
        with self._lock:
            return self._current().record(company)

    def save_company(self, record: CompanyRecord) -> None:
        with self._lock:
            state = self._current()
            state.put(record)
            for uid in record.alerted:
                job = record.jobs[uid]
                state.alerts.append(
                    {"company": record.name, "title": job.title, "location": job.location, "url": job.url, "notified_at": job.notified_at}
                )
            del state.alerts[:-ALERT_LOG_SIZE]
            record.alerted = []
            self._persist()

    def record_run(self, runner: str, summary: dict) -> None:
        with self._lock:
            self._current().runs[runner] = dict(summary)
            self._persist()

    def last_runs(self) -> dict[str, dict]:
        with self._lock:
            return {runner: dict(s) for runner, s in self._current().runs.items()}

    def recent_alerts(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return [dict(a) for a in reversed(self._current().alerts[-limit:])]

    def load(self) -> MonitorState:
        with self._lock:
            return MonitorState.from_dict(self._current().to_dict())

    def save(self, state: MonitorState) -> None:
        with self._lock:
            self.state = MonitorState.from_dict(state.to_dict())
            self._persist()


class JsonStateStore(MemoryStateStore):
    """The whole state in one JSON file, read on first use and rewritten on every change."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._loaded = False

    def _current(self) -> MonitorState:
        if not self._loaded:
            self.state, self._loaded = self._read(), True
        return self.state

    def _read(self) -> MonitorState:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return MonitorState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Refuse to continue: silently starting fresh would re-alert every job.
            raise RuntimeError(f"State file {self.path} is corrupt: {exc}") from exc
        return MonitorState.from_dict(data)

    def save(self, state: MonitorState) -> None:
        self._loaded = True  # the caller's state replaces whatever the file held
        super().save(state)

    def _persist(self) -> None:
        text = json.dumps(self.state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if self.path.exists() and self.path.read_text(encoding="utf-8") == text:
            log.debug("State unchanged; not rewriting %s", self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a crash mid-write can't corrupt the state.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".seen_jobs.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.debug("Saved state to %s", self.path)
