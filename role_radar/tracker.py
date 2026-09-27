"""New-job detection: compares one company's current listing to stored state.

A job is alerted exactly once: when it matches the filters and has never been
notified. The cases:

  new job                  → recorded; alerted if it matches
  existing job unchanged   → nothing happens
  job removed              → marked removed_at (only if the listing was complete)
  removed job returns      → same uid reappears: removed_at cleared, no new alert
  reposted with a new ID   → new uid, same fingerprint as a job notified before and
                             removed within `repost_window_days`: recorded, not alerted
  same title, many places  → different fingerprints (location is part of it), so
                             each is its own job; notifications group them together
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
    # uid → uid of a posting it duplicates that is being alerted in this same check.
    # Recorded as duplicate_of only once that alert has gone out (record_delivery):
    # if it fails and the other posting closes, this one must still alert.
    pending_duplicates: dict[str, str] = field(default_factory=dict)


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
    repost_window_days: int = 30,
    max_alert_age_days: int | None = None,
    now: datetime | None = None,
) -> CompanyDiff:
    """Update `state` in place for one company and return what changed.

    `matches` maps uid → filter result. With `notify=False` (baseline run),
    matching jobs are recorded as already notified so they never alert; so are
    jobs posted more than `max_alert_age_days` before now.
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

    window = timedelta(days=repost_window_days)
    # fingerprint → uid of the posting that "owns" it (already notified, or claimed this run)
    owners = {
        rec.fingerprint: uid
        for uid, rec in records.items()
        if rec.notified_at and (rec.active or (repost_window_days > 0 and now - from_iso(rec.removed_at) <= window))
    }
    for job in candidates:
        rec = records[job.uid]
        owner = owners.get(job.fingerprint)
        if owner and owner != job.uid:
            if records[owner].notified_at:
                rec.duplicate_of = owner
            else:  # claimed earlier in this check; final once its alert is delivered
                diff.pending_duplicates[job.uid] = owner
            kind = "repost of removed job" if records[owner].removed_at else "duplicate of open job"
            diff.suppressed.append((job, f"{kind} {owner}"))
            continue
        owners[job.fingerprint] = job.uid
        age = (now.date() - job.date_posted).days if job.date_posted else None
        if not notify:
            rec.notified_at = stamp
            diff.suppressed.append((job, "baseline run"))
        elif max_alert_age_days is not None and age is not None and age > max_alert_age_days:
            rec.notified_at = stamp
            diff.suppressed.append((job, f"posted {age} days ago"))
        else:
            diff.to_notify.append(job)
    return diff


def mark_notified(state: MonitorState, jobs: list[JobPosting], now: datetime | None = None) -> None:
    stamp = to_iso(now or utcnow())
    for job in jobs:
        rec = state.jobs_for(job.company).get(job.uid)
        if rec:
            rec.notified_at = stamp


def record_delivery(state: MonitorState, diff: CompanyDiff, now: datetime | None = None) -> None:
    """After the alerts in `diff` went out: mark them notified, and settle the duplicates they own."""
    mark_notified(state, diff.to_notify, now)
    settle_duplicates(state, diff)


def settle_duplicates(state: MonitorState, diff: CompanyDiff) -> None:
    """Record duplicate_of for the check's duplicates whose original has now been alerted."""
    records = state.jobs_for(diff.company)
    for uid, owner in diff.pending_duplicates.items():
        if uid in records and owner in records and records[owner].notified_at:
            records[uid].duplicate_of = owner
