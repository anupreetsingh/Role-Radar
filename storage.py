"""Persistence for seen jobs.

The rest of the program only talks to `StateStore.load()` / `StateStore.save()`
and the `MonitorState` object, so swapping JSON for SQLite, DynamoDB, etc. means
writing one new `StateStore` subclass.

The JSON file is written deterministically (sorted keys, no volatile
"last run" timestamps) so it only changes when jobs are added, removed or
notified — which keeps GitHub Actions from committing on every run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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

    @property
    def active(self) -> bool:
        return self.removed_at is None

    @classmethod
    def from_dict(cls, data: dict) -> SeenJob:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class MonitorState:
    # company name → job uid → record
    companies: dict[str, dict[str, SeenJob]] = field(default_factory=dict)

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

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "companies": {
                company: {uid: {k: v for k, v in asdict(job).items() if v not in (None, False)} for uid, job in jobs.items()}
                for company, jobs in self.companies.items()
                if jobs
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> MonitorState:
        companies = {
            company: {uid: SeenJob.from_dict(job) for uid, job in jobs.items()}
            for company, jobs in (data.get("companies") or {}).items()
        }
        return cls(companies=companies)


class StateStore(ABC):
    @abstractmethod
    def load(self) -> MonitorState: ...

    @abstractmethod
    def save(self, state: MonitorState) -> None: ...


class JsonStateStore(StateStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> MonitorState:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return MonitorState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Refuse to continue: silently starting fresh would re-alert every job.
            raise RuntimeError(f"State file {self.path} is corrupt: {exc}") from exc
        return MonitorState.from_dict(data)

    def save(self, state: MonitorState) -> None:
        text = json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if self.path.exists() and self.path.read_text(encoding="utf-8") == text:
            log.info("State unchanged; not rewriting %s", self.path)
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
        log.info("Saved state to %s", self.path)


class MemoryStateStore(StateStore):
    """In-memory store for tests and dry runs."""

    def __init__(self, state: MonitorState | None = None) -> None:
        self.state = state or MonitorState()
        self.saves = 0

    def load(self) -> MonitorState:
        return MonitorState.from_dict(self.state.to_dict())

    def save(self, state: MonitorState) -> None:
        self.state = MonitorState.from_dict(state.to_dict())
        self.saves += 1
