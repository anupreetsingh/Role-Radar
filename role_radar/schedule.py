"""Due-based scheduling: which companies to check now, and when to check each next.

A company is due once its next_check_at has passed, or if it has never been
checked. After a check, next_check_at = check time + interval, except:

- Failures back off: from the 3rd consecutive failure the interval doubles each
  time, up to 8x (4 hours at the default 30 minutes), so a broken site isn't hit
  every half hour forever. One success resets it.
- A check that ran more than half an interval late (a company's first check, or
  catching up after an outage) gets a random extra 0-1 interval. Without this, a
  burst of catch-up checks would come due together every half hour forever; with
  it, the burst spreads out across the half hour after one round.

The schedule lives in the shared store, so whichever runner holds the lease
picks up exactly what's due: a handoff continues where the other side stopped.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable, Iterable, Union

from role_radar.config import CompanyConfig, Settings
from role_radar.scrapers import ats_name
from role_radar.storage import CompanyMeta, from_iso, to_iso

MAX_BACKOFF = 8  # longest interval for a failing company, as a multiple of the normal one
_rng = random.Random()

# One interval for every company, or a function giving each company's own.
Interval = Union[timedelta, Callable[[CompanyConfig], timedelta]]


def interval_for(settings: Settings) -> Callable[[CompanyConfig], timedelta]:
    """Each company's check interval: its ATS's settings.check_interval_by_ats, else the default."""
    return lambda company: settings.check_interval_for(ats_name(company.url, company.ats))


def _interval(interval: Interval, company: CompanyConfig) -> timedelta:
    return interval if isinstance(interval, timedelta) else interval(company)


def due_at(meta: CompanyMeta | None, interval: timedelta) -> datetime | None:
    """When the company is next due, or None if it has never been checked."""
    if meta is None:
        return None
    if meta.next_check_at:
        return from_iso(meta.next_check_at)
    if meta.last_checked_at:
        return from_iso(meta.last_checked_at) + interval
    return None


def due_companies(
    companies: Iterable[CompanyConfig], schedule: dict[str, CompanyMeta], now: datetime, interval: Interval
) -> list[CompanyConfig]:
    """Companies due at `now`: never-checked ones first (in config order), then the most overdue."""
    due = []
    for company in companies:
        at = due_at(schedule.get(company.name), _interval(interval, company))
        if at is None or at <= now:
            due.append((at is not None, at or now, company))
    due.sort(key=lambda d: d[:2])
    return [company for *_, company in due]


def next_due(
    companies: Iterable[CompanyConfig], schedule: dict[str, CompanyMeta], now: datetime, interval: Interval
) -> datetime | None:
    """The earliest time any of `companies` is due (`now` if one never was checked); None if there are none."""
    times = [due_at(schedule.get(c.name), _interval(interval, c)) or now for c in companies]
    return min(times, default=None)


def after_check(
    meta: CompanyMeta,
    checked_at: datetime,
    interval: timedelta,
    error: str | None = None,
    rng: random.Random | None = None,
) -> CompanyMeta:
    """The company's schedule after a check that started at `checked_at`."""
    was_due = due_at(meta, interval)
    failures = meta.failures + 1 if error else 0
    delay = interval * min(2 ** max(failures - 2, 0), MAX_BACKOFF)
    if was_due is None or checked_at - was_due > interval / 2:
        delay += interval * (rng or _rng).random()
    return CompanyMeta(
        last_checked_at=to_iso(checked_at),
        next_check_at=to_iso((checked_at + delay).replace(microsecond=0)),
        failures=failures,
        last_error=error[:300] if error else None,
        last_ok_at=meta.last_ok_at if error else to_iso(checked_at),
    )
