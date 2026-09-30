"""One pass over the companies that are due: scrape, detect new jobs, alert, save.

Flow of a pass:
  load config + every company's schedule → pick the companies that are due
  → for each due company (concurrently, bounded):
      load its state → scrape listing → fetch details only for unseen jobs whose
      filter needs a field the listing lacks (and that could still match)
      → filter → diff against state → read each new match's description once and
      drop those asking for too much experience (filter.max_experience_years)
      → queue its new matches (or alert immediately if digests are disabled), to
        the alert channels switched on; with all of them off they stay queued
      → save its state and next check time straight away (fenced on the lease)
  → meanwhile, every TICK seconds: report progress for Live Tracking, and send
    the shared digest if it's due (leaving out companies being checked right then)
  → at the end, send the digest if it's due

If the lease turns out to be lost, the pass stops without saving or sending
anything more. The companies it didn't finish are still due, so whoever holds
the lease picks them up. runner.py decides when passes run and holds the lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar, Union

from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.digest import flush_digest
from role_radar.experience import assess
from role_radar.http_client import HttpClient, RobotsCache, format_bytes
from role_radar.lease import Lease, LeaseLost, LocalLease
from role_radar.models import JobPosting
from role_radar.notifications import ConsoleNotifier, Notifier, notify_all, switched_on
from role_radar.schedule import after_check, due_companies, interval_for, next_due, next_quick, quick_due
from role_radar.scrapers import BaseScraper, ats_name, scraper_class_for
from role_radar.storage import (
    SKIPPED, CompanyMeta, CompanyRecord, MonitorState, QueuedMatch, SeenJob, StateStore, alerts_off, to_iso, utcnow,
)
from role_radar.tracker import CompanyDiff, dedupe, mark_notified, reconcile

log = logging.getLogger("monitor")

T = TypeVar("T")
# A list of channels, or a function that builds them the first time they're needed.
Notifiers = Union[Sequence[Notifier], Callable[[], Sequence[Notifier]]]

EXIT_OK, EXIT_FAILED, EXIT_NOTHING, EXIT_LEASE_LOST = 0, 1, 2, 3
# Seconds kept free before a deadline, so a company that finishes late can still
# fetch secrets, alert (Discord may ask us to wait) and save (with retries).
SAVE_MARGIN = 120.0
SAVE_RETRIES_AFTER_ALERTS = 4  # extra save attempts once alerts have gone out...
SAVE_RETRY_BASE = 5.0  # ...5, 10, 20, 40 s apart
UNSAVED_HOLD = 120.0  # a company whose save failed isn't retried for this long
TICK = 15.0  # during a pass: how often to report progress and see whether the digest is due


class Unsaved:
    """What this runner couldn't save, so it neither re-sends alerts nor retries at once.

    A failed save leaves the company due, and its next check would send the
    same alerts again. This remembers them for the life of the process (a warm
    Lambda keeps it between runs).
    """

    def __init__(self) -> None:
        self.sent: dict[str, dict[str, str]] = {}  # company → uid → notified_at: alerted, not saved
        self.channels: dict[str, dict[str, dict[str, str]]] = {}
        self._retry_at: dict[str, float] = {}

    def failed(self, company: str, record: CompanyRecord) -> None:
        alerted = {uid: record.jobs[uid].notified_at for uid in record.alerted if uid in record.jobs}
        self.sent.setdefault(company, {}).update({uid: at for uid, at in alerted.items() if at})
        self.channels[company] = {uid: dict(job.notified_channels) for uid, job in record.jobs.items() if job.notified_channels}
        self._retry_at[company] = time.monotonic() + UNSAVED_HOLD

    def saved(self, company: str) -> None:
        self.sent.pop(company, None)
        self.channels.pop(company, None)
        self._retry_at.pop(company, None)

    def holding(self, company: str) -> bool:
        return self._retry_at.get(company, 0.0) > time.monotonic()


@dataclass
class CompanyOutcome:
    company: str
    error: str | None = None
    listed: int = 0
    matched: list[JobPosting] = field(default_factory=list)
    diff: CompanyDiff | None = None
    delivered: bool = True  # False when any configured channel still needs the alerts
    meta: CompanyMeta | None = None  # the schedule saved after this check
    skipped: bool = False  # due, but not started (stopping, or out of time)
    queued: bool = False  # matches left pending: for the digest, or while alerts are switched off
    quick: bool = False  # a quick check (settings.quick_check_by_ats), not a full one
    top_uids: list[str] | None = None  # the listing's newest page, for the next quick check

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def alerted(self) -> int:
        return len(self.diff.to_notify) if self.diff and self.delivered and not self.queued else 0


@dataclass
class PassResult:
    """What one pass did, for logs, `status` and exit codes."""

    enabled: int = 0
    outcomes: list[CompanyOutcome] = field(default_factory=list)
    lease_lost: bool = False
    seconds: float = 0.0
    requests: int = 0
    bytes: int = 0
    next_due: datetime | None = None
    finished_at: datetime | None = None
    nothing_configured: bool = False
    digest_attempted: bool = False  # these four add up every digest the pass sent
    digest_jobs: int = 0
    digest_completed: int = 0
    digest_failed: bool = False
    failing: int | None = None  # enabled companies whose last check failed, after this pass
    pending: int | None = None  # matches waiting to be sent (the digest, or alerts switched off)

    @property
    def checked(self) -> list[CompanyOutcome]:
        return [o for o in self.outcomes if not o.skipped]

    @property
    def failed(self) -> list[CompanyOutcome]:
        return [o for o in self.checked if not o.ok]

    @property
    def alerts(self) -> int:
        return sum(o.alerted for o in self.outcomes) + self.digest_completed

    @property
    def undelivered(self) -> list[CompanyOutcome]:
        return [o for o in self.outcomes if not o.delivered]

    @property
    def exit_code(self) -> int:
        if self.nothing_configured:
            return EXIT_NOTHING
        if self.lease_lost:
            return EXIT_LEASE_LOST
        checked = self.checked
        if (checked and len(self.failed) == len(checked)) or self.undelivered or self.digest_failed:
            return EXIT_FAILED
        return EXIT_OK

    def summary(self) -> dict[str, Any]:
        """For the store's run log (`role-radar status`)."""
        return {
            "finished_at": to_iso(self.finished_at or utcnow()),
            "checked": len(self.checked),
            "failed": len(self.failed),
            "alerts": self.alerts,
            "undelivered": len(self.undelivered) + int(self.digest_failed),
            "digest_attempted": self.digest_attempted,
            "digest_jobs": self.digest_jobs,
            "skipped": len(self.outcomes) - len(self.checked),
            "seconds": round(self.seconds, 1),
            "requests": self.requests,
            "bytes": self.bytes,
            "lease_lost": self.lease_lost,
            "failing": self.failing,
            "pending": self.pending,
        }

    def counts(self) -> dict[str, int]:
        """What this pass did, added to its runner's hourly activity (`status`, the menu bar app)."""
        diffs = [o.diff for o in self.checked if o.diff]
        return {
            "checked": len(self.checked),
            "failed": len(self.failed),
            "new_jobs": sum(len(d.new) for d in diffs),
            # New jobs that match; to_notify alone also holds matches still waiting from earlier checks.
            "matches": sum(len({j.uid for j in d.new} & {j.uid for j in d.to_notify}) for d in diffs),
            "alerts": self.alerts,
        }


def _needs_details(rec: SeenJob | None) -> bool:
    return rec is None or not (rec.notified_at or rec.duplicate_of or rec.detail_fetched)


async def _fetch_details(scraper: BaseScraper, job: JobPosting) -> bool:
    try:
        await scraper.fetch_details(job)
        return True
    except Exception as exc:
        log.warning("[%s] details failed for %r: %s", job.company, job.title, exc)
        return False


async def _read_description(scraper: BaseScraper, job: JobPosting) -> str | None:
    try:
        return await scraper.fetch_description(job)
    except Exception as exc:
        log.warning("[%s] couldn't read the description of %r (%s); keeping it", job.company, job.title, exc)
        return None


async def _screen_experience(company: CompanyConfig, scraper: BaseScraper, state: MonitorState,
                             diff: CompanyDiff, settings: Settings) -> None:
    """Drop the check's new matches whose description asks for more experience than the filter allows.

    Each match's description is read once. Where that's a request per job, at most
    max_detail_requests are read a check; the rest wait (unmatched, so the digest skips
    them) for the next check. A dropped match is recorded as notified, with the reason,
    so it never alerts. One whose description can't be read is kept.
    """
    records = state.jobs_for(company.name)
    todo = [j for j in diff.to_notify if not records[j.uid].experience_checked]
    held: list[JobPosting] = []
    if scraper.description_costs_request and len(todo) > settings.max_detail_requests:
        log.info("[%s] %d new matches to check for experience; checking %d this run",
                 company.name, len(todo), settings.max_detail_requests)
        todo, held = todo[: settings.max_detail_requests], todo[settings.max_detail_requests :]
    texts = await asyncio.gather(*(_read_description(scraper, j) for j in todo))
    stamp = to_iso(utcnow())
    skip = {j.uid for j in held}
    for job, text in zip(todo, texts):
        rec = records[job.uid]
        rec.experience_checked = True
        verdict = assess(text, company.filter.max_experience_years, company.filter.education)
        log.debug("[%s] %s: %s", company.name, job.title, verdict.reason)
        if not verdict.keep:
            rec.notified_at, rec.dropped_for = stamp, verdict.reason
            diff.suppressed.append((job, verdict.reason))
            skip.add(job.uid)
    for job in held:
        records[job.uid].matched = False
    diff.to_notify = [j for j in diff.to_notify if j.uid not in skip]


async def _bounded(
    limit: int, jobs: Iterable[Callable[[], Awaitable[T]]], gates: Sequence[asyncio.Semaphore | None] | None = None
) -> list[T]:
    """Run each job, at most `limit` at a time, returning results in order.

    A job with a gate waits for it before taking one of the `limit` slots, so jobs
    queued behind a narrow gate don't hold slots other jobs could use.
    If one raises, the others are cancelled and the exception propagates.
    """
    sem = asyncio.Semaphore(limit)

    async def one(job: Callable[[], Awaitable[T]], gate: asyncio.Semaphore | None) -> T:
        async with gate or contextlib.nullcontext():
            async with sem:
                return await job()

    jobs = list(jobs)
    tasks = [asyncio.ensure_future(one(job, gate)) for job, gate in zip(jobs, gates or [None] * len(jobs))]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _ats_gates(companies: Sequence[CompanyConfig], settings: Settings) -> list[asyncio.Semaphore | None]:
    """Each company's gate from settings.company_concurrency_by_ats (shared per ATS), or None."""
    shared = {ats: asyncio.Semaphore(n) for ats, n in settings.company_concurrency_by_ats.items()}
    return [shared.get(ats_name(c.url, c.ats) or "") for c in companies]


async def check_company(
    company: CompanyConfig,
    http: HttpClient,
    state: MonitorState,
    settings: Settings,
    *,
    notify: bool,
    first_run: bool | None = None,
    scraper: BaseScraper | None = None,
    first_page: list[JobPosting] | None = None,
) -> CompanyOutcome:
    """Scrape one company and reconcile the listing into `state` (in memory only).

    With `first_page` (a quick check that saw new jobs on it), only the newest
    jobs are read, as a partial listing.
    """
    scraper = scraper or scraper_class_for(company.url, company.ats)(company, http)
    scraper.known = state.companies.get(company.name, {})
    result = await (scraper.fetch_newest(first_page) if first_page is not None else scraper.fetch_jobs())
    top_uids = [j.freeze_identity().uid for j in result.first_page] if result.first_page is not None else None
    jobs = dedupe([j.freeze_identity() for j in result.jobs if j.title])
    log.debug("[%s] %d job(s) listed via %s", company.name, len(jobs), scraper.name)

    # Only fetch detail pages when the filter needs a field the listing lacks, for
    # jobs that could still match and haven't been handled.
    records = state.companies.get(company.name, {})
    todo = [
        j
        for j in jobs
        if scraper.wants_details(j)
        and _needs_details(records.get(j.uid))
        and company.filter.could_match(j, scraper.missing_fields(j))
    ]
    fetched: set[str] = set()
    if todo:
        if len(todo) > settings.max_detail_requests:
            log.info("[%s] %d jobs need details; fetching %d this run", company.name, len(todo), settings.max_detail_requests)
            todo = todo[: settings.max_detail_requests]
        results = await asyncio.gather(*(_fetch_details(scraper, j) for j in todo))
        fetched = {j.uid for j, ok in zip(todo, results) if ok}

    matches: dict[str, bool] = {}
    matched: list[JobPosting] = []
    for job in jobs:
        verdict = company.filter.evaluate(job)
        matches[job.uid] = verdict.matched
        if verdict:
            matched.append(job)
        log.debug("[%s] %s: %s", company.name, job.title, verdict.reason)

    if first_run is None:
        first_run = state.is_new_company(company.name)
    diff = reconcile(
        state,
        company.name,
        jobs,
        matches,
        complete=result.complete,
        detail_fetched=fetched,
        notify=notify and (settings.notify_on_first_run or not first_run),
        max_alert_age_days=company.max_alert_age_days,
    )
    if (company.filter.max_experience_years is not None or company.filter.education) and diff.to_notify:
        await _screen_experience(company, scraper, state, diff, settings)
    changed = diff.new or diff.removed or diff.returned or diff.suppressed or diff.to_notify
    log.log(
        logging.INFO if changed else logging.DEBUG,
        "[%s] listed=%d matched=%d new=%d alert=%d removed=%d returned=%d suppressed=%d",
        company.name, len(jobs), len(matched), len(diff.new), len(diff.to_notify),
        len(diff.removed), len(diff.returned), len(diff.suppressed),
    )  # fmt: skip
    for job, reason in diff.suppressed:
        log.info("[%s] not alerting %r (%s): %s", company.name, job.title, job.location, reason)
    return CompanyOutcome(company.name, listed=len(jobs), matched=matched, diff=diff, top_uids=top_uids)


async def process_company(
    company: CompanyConfig,
    http: HttpClient,
    store: StateStore,
    notifiers: Callable[[], Sequence[Notifier]],
    settings: Settings,
    *,
    now: datetime,
    lease: Lease,
    notify: bool = True,
    dry_run: bool = False,
    timeout: float | None = None,
    unsaved: Unsaved | None = None,
    save_lock: asyncio.Lock | None = None,
    deadline: float | None = None,
    scraper: BaseScraper | None = None,
    first_page: list[JobPosting] | None = None,
) -> CompanyOutcome:
    """Check one company, alert on its new matches, then save its state right away.

    A failed check is saved too (its failure count and next check time), so a
    broken site waits for its next slot instead of being retried immediately.
    Raises LeaseLost, without sending or saving, if this runner lost the lease.
    """
    record = await asyncio.to_thread(store.load_company, company.name)
    state = MonitorState({company.name: record.jobs})
    try:
        outcome = await asyncio.wait_for(
            check_company(company, http, state, settings, notify=notify, first_run=record.is_new,
                          scraper=scraper, first_page=first_page),
            timeout=timeout or settings.company_timeout,
        )
    except Exception as exc:  # one broken company must not stop the run
        msg = str(exc) or type(exc).__name__
        log.error("[%s] failed: %s", company.name, msg)
        outcome = CompanyOutcome(company.name, error=msg)

    diff = outcome.diff
    if diff:
        if unsaved and not dry_run:
            for uid, channels in unsaved.channels.get(company.name, {}).items():
                if uid in record.jobs:
                    record.jobs[uid].notified_channels.update(channels)
                    record.delivery_changed = True
            _skip_already_sent(diff, record, unsaved.sent.get(company.name, {}))
        if diff.to_notify:
            if settings.digest_interval_minutes and not dry_run:
                outcome.queued = True
                log.info("[%s] %d match(es) pending the next digest", company.name, len(diff.to_notify))
            else:
                switches = {} if dry_run else await asyncio.to_thread(store.load_switches)
                if alerts_off(switches):
                    outcome.queued = True
                    log.info("[%s] alerts are switched off; %d match(es) wait until one is back on",
                             company.name, len(diff.to_notify))
                else:
                    _apply_skips(diff, record, [] if dry_run else await asyncio.to_thread(store.load_queue), now)
                    if diff.to_notify:
                        outcome.delivered = await _alert(diff, state, record, notifiers, lease, dry_run, switches)

    pruned = state.prune(settings.retention_days, now)
    if pruned:
        log.info("[%s] pruned %d long-removed job(s)", company.name, pruned)
    ats = ats_name(company.url, company.ats)
    quick_interval = settings.quick_interval_for(ats)
    if first_page is not None:  # a quick check leaves the full check's schedule as it was
        outcome.quick = True
    else:
        previous = record.meta
        record.meta = after_check(previous, now, settings.check_interval_for(ats), error=outcome.error)
        record.meta.top_uids = previous.top_uids
    if quick_interval:
        record.meta.next_quick_at = to_iso(now + quick_interval)
        if outcome.top_uids is not None:
            record.meta.top_uids = outcome.top_uids
    outcome.meta = record.meta
    if not dry_run:
        await _save(store, record, lease, save_lock, deadline, unsaved)
    return outcome


async def quick_check(
    company: CompanyConfig,
    http: HttpClient,
    store: StateStore,
    notifiers: Callable[[], Sequence[Notifier]],
    settings: Settings,
    *,
    meta: CompanyMeta,
    now: datetime,
    lease: Lease,
    dry_run: bool = False,
    timeout: float | None = None,
    save_lock: asyncio.Lock | None = None,
    **kwargs: Any,
) -> CompanyOutcome:
    """Between full checks: read the listing's newest page, and check further only if it changed.

    Most quick checks are one request and a schedule-row write, without reading
    the company's stored jobs: the page shows the same jobs as at the last check.
    Otherwise the company is checked like process_company does, reading pages
    from the top only until one holds a job already known. `meta` is the
    company's schedule row as loaded at the start of this pass.
    """
    scraper = scraper_class_for(company.url, company.ats)(company, http)
    try:
        page = await asyncio.wait_for(scraper.fetch_first_page(), timeout=timeout or settings.company_timeout)
    except Exception as exc:  # the next quick (or full) check tries again
        error = str(exc) or type(exc).__name__
        log.warning("[%s] quick check failed: %s", company.name, error)
        page = None
    else:
        error = None
        top = [job.freeze_identity().uid for job in page]
        if not set(top) <= set(meta.top_uids or ()):
            return await process_company(company, http, store, notifiers, settings, now=now, lease=lease, dry_run=dry_run,
                                         timeout=timeout, save_lock=save_lock, scraper=scraper, first_page=page, **kwargs)
    interval = settings.quick_interval_for(ats_name(company.url, company.ats)) or settings.check_interval
    meta = replace(meta, next_quick_at=to_iso(now + interval))
    if not dry_run:
        lease.check()
        async with save_lock or contextlib.nullcontext():
            await asyncio.to_thread(store.save_meta, company.name, meta)
    return CompanyOutcome(company.name, error=error, meta=meta, quick=True, listed=len(page or ()))


def _apply_skips(diff: CompanyDiff, record: CompanyRecord, listed: list[QueuedMatch], now: datetime) -> None:
    """Matches skipped in Live Tracking (without a digest, while alerts were off): record them instead of alerting."""
    skipped = {m.uid for m in listed if m.company == record.name and m.skipped_at and not m.done_at}
    for job in [j for j in diff.to_notify if j.uid in skipped]:
        rec = record.jobs[job.uid]
        rec.notified_at, rec.dropped_for = to_iso(now), SKIPPED
        diff.suppressed.append((job, SKIPPED))
        log.info("[%s] not alerting %r (%s): %s", diff.company, job.title, job.location, SKIPPED)
    diff.to_notify = [j for j in diff.to_notify if j.uid not in skipped]


def _skip_already_sent(diff: CompanyDiff, record: CompanyRecord, sent: dict[str, str]) -> None:
    """Jobs this runner alerted before a failed save: record them as notified instead of sending again."""
    already = [j for j in diff.to_notify if j.uid in sent]
    if not already:
        return
    log.info("[%s] not re-sending %d alert(s) that went out before a failed save", diff.company, len(already))
    for job in already:
        record.jobs[job.uid].notified_at = sent[job.uid]
    diff.to_notify = [j for j in diff.to_notify if j.uid not in sent]
    record.alerted += [j.uid for j in already]


async def _alert(
    diff: CompanyDiff,
    state: MonitorState,
    record: CompanyRecord,
    notifiers: Callable[[], Sequence[Notifier]],
    lease: Lease,
    dry_run: bool,
    switches: dict[str, bool],
) -> bool:
    """Send one company's matches to the channels switched on; keep partial successes for the next check."""
    # Fetch the channels (secrets) first, so nothing slow sits between the lease check and sending.
    try:
        channels = switched_on(await asyncio.to_thread(notifiers), switches)
    except Exception as exc:
        log.error("[%s] couldn't load notification settings (%s); alerts stay pending", diff.company, type(exc).__name__)
        invalidate = getattr(notifiers, "invalidate", None)
        if invalidate:
            invalidate()
        return False
    if not dry_run:
        # Never alert without the lease: after a pause, another runner may already have.
        lease.check()
        await asyncio.to_thread(lease.verify)
    before = {j.uid: dict(record.jobs[j.uid].notified_channels) for j in diff.to_notify}
    delivered = await notify_all(channels, diff.to_notify, check=None if dry_run else lease.check, records=record.jobs)
    record.delivery_changed |= any(record.jobs[uid].notified_channels != seen for uid, seen in before.items())
    completed = [j for j in diff.to_notify if channels and all(n.name in record.jobs[j.uid].notified_channels for n in channels)]
    mark_notified(state, completed)
    record.alerted += [j.uid for j in completed]
    if not delivered:
        log.error("[%s] notification delivery incomplete; failed channels will be retried next check", diff.company)
        invalidate = getattr(notifiers, "invalidate", None)
        if invalidate:  # re-read the channel settings next time, in case they changed
            invalidate()
    return delivered


async def _save(
    store: StateStore,
    record: CompanyRecord,
    lease: Lease,
    lock: asyncio.Lock | None,
    deadline: float | None,
    unsaved: Unsaved | None,
) -> None:
    """Save the company, trying harder once alerts have gone out: unsaved, they'd be sent again."""
    attempts = 1 + (SAVE_RETRIES_AFTER_ALERTS if record.alerted or record.delivery_changed else 0)
    record.meta.pending = record.pending_count()
    for attempt in range(attempts):
        lease.check()  # fail fast; the store also checks the stored lease in the same transaction
        try:
            async with lock or contextlib.nullcontext():
                await asyncio.to_thread(store.save_company, record)
        except LeaseLost:
            raise
        except Exception as exc:
            wait = min(SAVE_RETRY_BASE * 2**attempt, 60.0)
            out_of_time = deadline is not None and time.monotonic() + wait > deadline
            if attempt == attempts - 1 or out_of_time:
                if unsaved:
                    unsaved.failed(record.name, record)
                raise
            log.warning("[%s] couldn't save (%s); its alerts already went out, so retrying in %.0fs", record.name, exc, wait)
            await asyncio.sleep(wait)
        else:
            if unsaved:
                unsaved.saved(record.name)
            return


async def run_pass(
    config: AppConfig,
    store: StateStore,
    notifiers: Notifiers,
    *,
    lease: Lease | None = None,
    dry_run: bool = False,
    baseline: bool = False,
    only: set[str] | None = None,
    check_all: bool = False,
    should_stop: Callable[[], bool | Awaitable[bool]] | None = None,
    deadline: float | None = None,
    robots: RobotsCache | None = None,
    unsaved: Unsaved | None = None,
    clock: Callable[[], datetime] = utcnow,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> PassResult:
    """Check the companies that are due (all of them with check_all, baseline or only).

    The caller must already hold `lease` (a LocalLease is used when none is given).
    No new company starts once `should_stop()` (sync or async) returns true or
    `deadline` (time.monotonic) is near; companies already started finish and
    are saved. Saves are made one at a time, so this runner's transactions
    don't conflict with each other on the lease they all check. `progress` is
    called (in a thread) with how far a pass with work to do has got.
    """
    settings = config.settings
    result = PassResult()
    companies = [c for c in config.companies if c.enabled and (not only or c.name.lower() in only)]
    result.enabled = len(companies)
    if not companies:
        log.error("No companies to check")
        result.nothing_configured = True
        return result
    if lease is None:
        lease = LocalLease("local")
        lease.acquire(0)

    started = time.monotonic()
    schedule = await asyncio.to_thread(store.load_schedule)
    due = companies if (check_all or baseline or only) else due_companies(companies, schedule, clock(), interval_for(settings))
    # Quick checks go first: each is usually one request, and shouldn't wait behind full checks.
    quick = [] if (check_all or baseline or only) else quick_due(companies, schedule, clock(), settings)
    if not due and not quick:
        result.next_due = _next_due(companies, schedule, clock(), settings)
        if not baseline and not dry_run:
            await _digest(config, store, notifiers, lease, result, clock(), deadline, unsaved)
        _health(result, config, schedule)
        result.seconds = time.monotonic() - started
        result.finished_at = clock()
        log.debug("Nothing due; next check at %s", to_iso(result.next_due) if result.next_due else "-")
        return result

    channels = _channels(notifiers, dry_run)
    save_lock = asyncio.Lock()
    locks: dict[str, asyncio.Lock] = {}  # per company: its check, or the digest while it has the company loaded
    done = 0

    async def guarded(company: CompanyConfig, quick_meta: CompanyMeta | None = None) -> CompanyOutcome:
        nonlocal done
        async with locks.setdefault(company.name, asyncio.Lock()):
            try:
                return await check(company, quick_meta)
            finally:
                done += 1

    async def check(company: CompanyConfig, quick_meta: CompanyMeta | None) -> CompanyOutcome:
        timeout = settings.company_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic() - SAVE_MARGIN)
        if timeout <= 0 or (unsaved and unsaved.holding(company.name)) or await _stopping(should_stop):
            return CompanyOutcome(company.name, skipped=True)
        try:
            if quick_meta is not None:
                return await quick_check(
                    company, http, store, channels, settings,
                    meta=quick_meta, now=clock(), lease=lease, notify=not baseline, dry_run=dry_run, timeout=timeout,
                    unsaved=unsaved, save_lock=save_lock, deadline=deadline,
                )  # fmt: skip
            return await process_company(
                company, http, store, channels, settings,
                now=clock(), lease=lease, notify=not baseline, dry_run=dry_run, timeout=timeout,
                unsaved=unsaved, save_lock=save_lock, deadline=deadline,
            )  # fmt: skip
        except LeaseLost:
            raise
        except Exception as exc:  # loading or saving state failed; the company stays due
            msg = str(exc) or type(exc).__name__
            log.error("[%s] state could not be loaded or saved: %s", company.name, msg)
            return CompanyOutcome(company.name, error=msg)

    work = [(c, schedule[c.name]) for c in quick] + [(c, None) for c in due]
    gates = _ats_gates([c for c, _ in work], settings)
    round_info = {"started_at": to_iso(clock()), "total": len(work)}
    finished = asyncio.Event()

    async def report(**extra: Any) -> None:
        if progress and not dry_run:
            try:
                await asyncio.to_thread(progress, {**round_info, "done": done, "updated_at": to_iso(clock()), **extra})
            except Exception as exc:  # informational only
                log.debug("Couldn't report progress: %s", exc)

    async def tick() -> None:
        """Until the companies are done: progress, and the digest when it's due (it isn't cancelled mid-send)."""
        while not finished.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(finished.wait(), TICK)
            if finished.is_set() or result.lease_lost:
                return
            await report()
            if not baseline and not dry_run:
                await _digest(config, store, notifiers, lease, result, clock(), deadline, unsaved, locks, save_lock)

    await report()
    ticker = asyncio.create_task(tick())
    async with HttpClient(settings.http, robots=robots) as http:
        try:
            result.outcomes = await _bounded(
                settings.max_company_concurrency, [lambda c=c, m=m: guarded(c, m) for c, m in work], gates
            )
        except LeaseLost as exc:
            log.error("Stopped: %s. Unfinished companies are still due for whoever holds the lease.", exc)
            result.lease_lost = True
        finally:
            finished.set()
            await ticker

    result.requests, result.bytes = http.stats.requests, http.stats.bytes
    schedule.update({o.company: o.meta for o in result.outcomes if o.meta})
    result.next_due = _next_due(companies, schedule, clock(), settings)
    # Sent even once should_stop() is true: it stops new companies, not the digest. Otherwise a
    # Lambda with a backlog, which always works to the end of its window, would never send one.
    if not baseline and not dry_run and not result.lease_lost:
        await _digest(config, store, notifiers, lease, result, clock(), deadline, unsaved)
    _health(result, config, schedule)
    result.seconds = time.monotonic() - started
    result.finished_at = clock()
    await report(finished_at=to_iso(result.finished_at))
    _log_pass(result, len(work), http, dry_run)
    write_step_summary(result.outcomes, result.alerts)
    return result


def _health(result: PassResult, config: AppConfig, schedule: dict[str, CompanyMeta]) -> None:
    """Failing companies and matches waiting to be sent, from the schedule as this pass left it."""
    metas = [schedule[c.name] for c in config.companies if c.enabled and c.name in schedule]
    result.failing = sum(1 for meta in metas if meta.failures)
    # The digest's own saves don't reach `schedule`: take off what it sent.
    result.pending = max(0, sum(meta.pending or 0 for meta in metas) - result.digest_completed)


async def _digest(
    config: AppConfig,
    store: StateStore,
    notifiers: Notifiers,
    lease: Lease,
    result: PassResult,
    now: datetime,
    deadline: float | None,
    unsaved: Unsaved | None,
    locks: dict[str, asyncio.Lock] | None = None,
    save_lock: asyncio.Lock | None = None,
) -> None:
    if not config.settings.digest_interval_minutes:
        return
    if deadline is not None and time.monotonic() + SAVE_MARGIN >= deadline:
        return

    async def save(record: CompanyRecord) -> None:
        await _save(store, record, lease, save_lock, deadline, unsaved)

    try:
        digest = await flush_digest(
            store, [c.name for c in config.companies if c.enabled], _channels(notifiers, False), lease,
            now, config.settings.digest_interval_minutes, save, deadline=deadline,
            sent=unsaved.sent if unsaved else None, receipts=unsaved.channels if unsaved else None,
            locks=locks, save_lock=save_lock,
        )
        result.digest_attempted |= digest.attempted
        result.digest_jobs += digest.jobs
        result.digest_completed += digest.completed
        result.digest_failed |= digest.failed
        if digest.next_due:
            result.next_due = min(result.next_due, digest.next_due) if result.next_due else digest.next_due
    except LeaseLost as exc:
        result.lease_lost = True
        log.error("Digest stopped: %s", exc)
    except Exception as exc:
        result.digest_failed = True
        log.error("Digest failed (%s); undelivered jobs remain pending", type(exc).__name__)


async def _stopping(should_stop: Callable[[], bool | Awaitable[bool]] | None) -> bool:
    if should_stop is None:
        return False
    result = should_stop()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def _channels(notifiers: Notifiers, dry_run: bool) -> Callable[[], Sequence[Notifier]]:
    if dry_run:
        return lambda: [ConsoleNotifier()]
    return notifiers if callable(notifiers) else (lambda: notifiers)


def _next_due(companies: list[CompanyConfig], schedule: dict[str, CompanyMeta], now: datetime, settings: Settings) -> datetime | None:
    """When the next full or quick check is due."""
    times = [t for t in (next_due(companies, schedule, now, interval_for(settings)), next_quick(companies, schedule, settings)) if t]
    return min(times, default=None)


def _log_pass(result: PassResult, due: int, http: HttpClient, dry_run: bool) -> None:
    skipped = len(result.outcomes) - len(result.checked)
    quick = sum(1 for o in result.checked if o.quick)
    log.info(
        "Checked %d of %d due companies%s in %.1fs: %d failed, %d alert(s)%s%s; %s",
        len(result.checked), due, f" ({quick} quick)" if quick else "", result.seconds, len(result.failed), result.alerts,
        f", {skipped} left for later" if skipped else "",
        " (dry run: nothing saved)" if dry_run else "",
        http.stats.summary(top=5),
    )  # fmt: skip
    for host, stats in http.stats.busiest():
        log.debug("  %s: %d requests, %s, %d failed", host, stats.requests, format_bytes(stats.bytes), stats.errors)
    if result.failed:
        log.info("Failed: %s", ", ".join(o.company for o in result.failed))


async def run(config: AppConfig, store: StateStore, notifiers: Notifiers, *, list_matches: bool = False, **kwargs: Any) -> int:
    """run_pass(), or print_matches() with list_matches, returning an exit code."""
    if list_matches:
        companies = [c for c in config.companies if c.enabled and (not kwargs.get("only") or c.name.lower() in kwargs["only"])]
        return await print_matches(companies, config.settings)
    return (await run_pass(config, store, notifiers, **kwargs)).exit_code


async def print_matches(companies: list[CompanyConfig], settings: Settings) -> int:
    """Print every job matching right now. Reads no state, sends nothing, saves nothing."""
    if not companies:
        log.error("No companies to check")
        return EXIT_NOTHING

    async def one(company: CompanyConfig) -> CompanyOutcome:
        try:
            return await asyncio.wait_for(
                check_company(company, http, MonitorState(), settings, notify=False), timeout=settings.company_timeout
            )
        except Exception as exc:
            log.error("[%s] failed: %s", company.name, str(exc) or type(exc).__name__)
            return CompanyOutcome(company.name, error=str(exc))

    gates = _ats_gates(companies, settings)
    async with HttpClient(settings.http) as http:
        outcomes = await _bounded(settings.max_company_concurrency, [lambda c=c: one(c) for c in companies], gates)
    for o in outcomes:
        for job in o.matched:
            print(f"{o.company} | {job.title} | {job.location or '-'} | {job.url}")
    return EXIT_OK if any(o.ok for o in outcomes) else EXIT_FAILED


def write_step_summary(outcomes: list[CompanyOutcome], alerted: int) -> None:
    """Append a Markdown table to the GitHub Actions job summary, if running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = ["| Company | Status | Listed | Matched | New alerts | Removed |", "|---|---|---|---|---|---|"]
    for o in outcomes:
        if o.skipped:
            continue
        status = "ok" if o.ok else f"error: {(o.error or '')[:80]}"
        d = o.diff
        rows.append(f"| {o.company} | {status} | {o.listed} | {len(o.matched)} | {o.alerted} | {len(d.removed) if d else 0} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"### Role Radar: {alerted} alert(s) sent\n\n" + "\n".join(rows) + "\n")
