"""New-job detection: compares one company's current listing to stored state.

A job is alerted exactly once: when it matches the filters and has never been
notified. Identity is the job's uid alone, so the cases are:

  new job                  → recorded; alerted if it matches
  existing job unchanged   → nothing happens
  job removed              → marked removed_at (only if the listing was complete)
  removed job returns      → same uid reappears: removed_at cleared, no new alert
  reposted with a new ID   → a new job: alerted if it matches, even with the same
                             title and location as an open or recently removed one
                             (it may be another vacancy, or a reopened search)
  same title, many places  → each is its own job; notifications group them together
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from role_radar.models import JobPosting
from role_radar.storage import MonitorState, SeenJob, from_iso, to_iso, utcnow


@dataclass
class CompanyDiff:
    company: str
    to_notify: list[JobPosting] = field(default_factory=list)
    new: list[JobPosting] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    suppressed: list[tuple[JobPosting, str]] = field(default_factory=list)  # (job, reason)
    unchanged: int = 0


def dedupe(jobs: list[JobPosting]) -> list[JobPosting]:
    """Drop repeated uids within one listing (e.g. overlapping pages)."""
    return list({job.uid: job for job in reversed(jobs)}.values())[::-1]


def reconcile(
    state: MonitorState,
    company: str,
    jobs: list[JobPosting],
    matches: dict[str, bool],
    *,
    complete: bool = True,
    detail_fetched: set[str] | None = None,
    notify: bool = True,
    max_alert_age_days: int | None = None,
    now: datetime | None = None,
) -> CompanyDiff:
    """Update `state` in place for one company and return what changed.

    `matches` maps uid → filter result. With `notify=False` (baseline run),
    matching jobs are recorded as already notified so they never alert; so are
    jobs posted more than `max_alert_age_days` before they were first seen (not
    before now: a match waiting while alerts are switched off still alerts).
    """
    now = now or utcnow()
    stamp = to_iso(now)
    detail_fetched = detail_fetched or set()
    records = state.jobs_for(company)
    diff = CompanyDiff(company)
    jobs = dedupe(jobs)
    current = {job.uid for job in jobs}

    candidates: list[JobPosting] = []
    for job in jobs:
        matched = matches.get(job.uid, False)
        rec = records.get(job.uid)
        if rec is None:
            rec = records[job.uid] = SeenJob(
                title=job.title, url=job.url, location=job.location, fingerprint=job.fingerprint, first_seen=stamp
            )
            diff.new.append(job)
        else:
            if rec.removed_at:
                rec.removed_at = None
                diff.returned.append(job.uid)
            else:
                diff.unchanged += 1
        rec.matched = matched
        rec.detail_fetched = rec.detail_fetched or job.uid in detail_fetched
        if matched and rec.notified_at is None and rec.duplicate_of is None:
            candidates.append(job)

    if complete:
        for uid, rec in records.items():
            if rec.active and uid not in current:
                rec.removed_at = stamp
                diff.removed.append(uid)

    for job in candidates:
        rec = records[job.uid]
        age = (from_iso(rec.first_seen).date() - job.date_posted).days if job.date_posted else None
        if not notify:
            rec.notified_at = stamp
            diff.suppressed.append((job, "baseline run"))
        elif max_alert_age_days is not None and age is not None and age > max_alert_age_days:
            rec.notified_at = stamp
            diff.suppressed.append((job, f"posted {age} days before it was found"))
        else:
            diff.to_notify.append(job)
    return diff


def mark_notified(state: MonitorState, jobs: list[JobPosting], now: datetime | None = None) -> None:
    stamp = to_iso(now or utcnow())
    for job in jobs:
        rec = state.jobs_for(job.company).get(job.uid)
        if rec:
            rec.notified_at = stamp
