"""One pass over the companies that are due: scrape, detect new jobs, alert, save.

Flow of a pass:
  load config + every company's schedule → pick the companies that are due
  → for each due company (concurrently, bounded):
      load its state → scrape listing → fetch details only for unseen jobs whose
      filter needs a field the listing lacks (and that could still match)
      → filter → diff against state
      → confirm the lease is still ours → alert on its new matches
      → save its state and next check time straight away (fenced on the lease)

If the lease turns out to be lost, the pass stops without saving or sending
anything more. The companies it didn't finish are still due, so whoever holds
the lease picks them up. runner.py decides when passes run and holds the lease.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar, Union

from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.http_client import HttpClient, RobotsCache, format_bytes
from role_radar.lease import Lease, LeaseLost, LocalLease
from role_radar.models import JobPosting
from role_radar.notifications import ConsoleNotifier, Notifier, notify_all
from role_radar.schedule import after_check, due_companies, next_due
from role_radar.scrapers import BaseScraper, scraper_class_for
from role_radar.storage import CompanyMeta, MonitorState, SeenJob, StateStore, to_iso, utcnow
from role_radar.tracker import CompanyDiff, dedupe, mark_notified, reconcile

log = logging.getLogger("monitor")

T = TypeVar("T")
# A list of channels, or a function that builds them the first time they're needed.
Notifiers = Union[Sequence[Notifier], Callable[[], Sequence[Notifier]]]

EXIT_OK, EXIT_FAILED, EXIT_NOTHING, EXIT_LEASE_LOST = 0, 1, 2, 3
SAVE_MARGIN = 20.0  # seconds kept free before a deadline for alerting and saving


@dataclass
class CompanyOutcome:
    company: str
    error: str | None = None
    listed: int = 0
    matched: list[JobPosting] = field(default_factory=list)
    diff: CompanyDiff | None = None
    delivered: bool = True  # False when there were alerts and no channel took them
    meta: CompanyMeta | None = None  # the schedule saved after this check
    skipped: bool = False  # due, but not started (stopping, or out of time)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def alerted(self) -> int:
        return len(self.diff.to_notify) if self.diff and self.delivered else 0


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

    @property
    def checked(self) -> list[CompanyOutcome]:
        return [o for o in self.outcomes if not o.skipped]

    @property
    def failed(self) -> list[CompanyOutcome]:
        return [o for o in self.checked if not o.ok]

    @property
    def alerts(self) -> int:
        return sum(o.alerted for o in self.outcomes)

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
        if (checked and len(self.failed) == len(checked)) or self.undelivered:
            return EXIT_FAILED
        return EXIT_OK

    def summary(self) -> dict[str, Any]:
        """For the store's run log (`role-radar status`)."""
        return {
            "finished_at": to_iso(self.finished_at or utcnow()),
            "checked": len(self.checked),
            "failed": len(self.failed),
            "alerts": self.alerts,
            "undelivered": len(self.undelivered),
            "skipped": len(self.outcomes) - len(self.checked),
            "seconds": round(self.seconds, 1),
            "requests": self.requests,
            "bytes": self.bytes,
            "lease_lost": self.lease_lost,
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


async def _bounded(limit: int, jobs: Iterable[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run each job, at most `limit` at a time, returning results in order.

    If one raises, the others are cancelled and the exception propagates.
    """
    sem = asyncio.Semaphore(limit)

    async def one(job: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await job()

    tasks = [asyncio.ensure_future(one(job)) for job in jobs]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def check_company(
    company: CompanyConfig,
    http: HttpClient,
    state: MonitorState,
    settings: Settings,
    *,
    notify: bool,
    first_run: bool | None = None,
) -> CompanyOutcome:
    """Scrape one company and reconcile the listing into `state` (in memory only)."""
    scraper = scraper_class_for(company.url, company.ats)(company, http)
    result = await scraper.fetch_jobs()
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
        repost_window_days=settings.repost_window_days,
    )
    changed = diff.new or diff.removed or diff.returned or diff.suppressed or diff.to_notify
    log.log(
        logging.INFO if changed else logging.DEBUG,
        "[%s] listed=%d matched=%d new=%d alert=%d removed=%d returned=%d suppressed=%d",
        company.name, len(jobs), len(matched), len(diff.new), len(diff.to_notify),
        len(diff.removed), len(diff.returned), len(diff.suppressed),
    )  # fmt: skip
    for job, reason in diff.suppressed:
        log.info("[%s] not alerting %r (%s): %s", company.name, job.title, job.location, reason)
    return CompanyOutcome(company.name, listed=len(jobs), matched=matched, diff=diff)


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
            check_company(company, http, state, settings, notify=notify, first_run=record.is_new),
            timeout=timeout or settings.company_timeout,
        )
    except Exception as exc:  # one broken company must not stop the run
        msg = str(exc) or type(exc).__name__
        log.error("[%s] failed: %s", company.name, msg)
        outcome = CompanyOutcome(company.name, error=msg)

    to_notify = outcome.diff.to_notify if outcome.diff else []
    if to_notify:
        if not dry_run:
            # Never alert without the lease: after a pause, another runner may already have.
            lease.check()
            await asyncio.to_thread(lease.verify)
        channels = await asyncio.to_thread(notifiers)
        outcome.delivered = await notify_all(list(channels), to_notify)
        if outcome.delivered:
            mark_notified(state, to_notify)
            record.alerted = [j.uid for j in to_notify]
        else:
            log.error("[%s] no notification channel succeeded; %d job(s) will be retried next check", company.name, len(to_notify))

    pruned = state.prune(settings.retention_days, now)
    if pruned:
        log.info("[%s] pruned %d long-removed job(s)", company.name, pruned)
    record.meta = outcome.meta = after_check(record.meta, now, settings.check_interval, error=outcome.error)
    if not dry_run:
        lease.check()  # fail fast; the store also checks the stored lease in the same transaction
        await asyncio.to_thread(store.save_company, record)
    return outcome


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
    should_stop: Callable[[], bool] | None = None,
    deadline: float | None = None,
    robots: RobotsCache | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> PassResult:
    """Check the companies that are due (all of them with check_all, baseline or only).

    The caller must already hold `lease` (a LocalLease is used when none is given).
    No new company starts once `should_stop()` returns true or `deadline`
    (time.monotonic) is near; companies already started finish and are saved.
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

    schedule = await asyncio.to_thread(store.load_schedule)
    due = companies if (check_all or baseline or only) else due_companies(companies, schedule, clock(), settings.check_interval)
    if not due:
        result.next_due = next_due(companies, schedule, clock(), settings.check_interval)
        log.debug("Nothing due; next check at %s", to_iso(result.next_due) if result.next_due else "-")
        return result

    started = time.monotonic()
    channels = _channels(notifiers, dry_run)

    async def guarded(company: CompanyConfig) -> CompanyOutcome:
        timeout = settings.company_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic() - SAVE_MARGIN)
        if timeout <= 0 or (should_stop and should_stop()):
            return CompanyOutcome(company.name, skipped=True)
        try:
            return await process_company(
                company, http, store, channels, settings,
                now=clock(), lease=lease, notify=not baseline, dry_run=dry_run, timeout=timeout,
            )  # fmt: skip
        except LeaseLost:
            raise
        except Exception as exc:  # loading or saving state failed; the company stays due
            msg = str(exc) or type(exc).__name__
            log.error("[%s] state could not be loaded or saved: %s", company.name, msg)
            return CompanyOutcome(company.name, error=msg)

    async with HttpClient(settings.http, robots=robots) as http:
        try:
            result.outcomes = await _bounded(settings.max_company_concurrency, [lambda c=c: guarded(c) for c in due])
        except LeaseLost as exc:
            log.error("Stopped: %s. Unfinished companies are still due for whoever holds the lease.", exc)
            result.lease_lost = True

    result.seconds = time.monotonic() - started
    result.requests, result.bytes = http.stats.requests, http.stats.bytes
    result.finished_at = clock()
    schedule.update({o.company: o.meta for o in result.outcomes if o.meta})
    result.next_due = next_due(companies, schedule, clock(), settings.check_interval)
    _log_pass(result, len(due), http, dry_run)
    write_step_summary(result.outcomes, result.alerts)
    return result


def _channels(notifiers: Notifiers, dry_run: bool) -> Callable[[], Sequence[Notifier]]:
    if dry_run:
        return lambda: [ConsoleNotifier()]
    return notifiers if callable(notifiers) else (lambda: notifiers)


def _log_pass(result: PassResult, due: int, http: HttpClient, dry_run: bool) -> None:
    skipped = len(result.outcomes) - len(result.checked)
    log.info(
        "Checked %d of %d due companies in %.1fs: %d failed, %d alert(s)%s%s; %s",
        len(result.checked), due, result.seconds, len(result.failed), result.alerts,
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

    async with HttpClient(settings.http) as http:
        outcomes = await _bounded(settings.max_company_concurrency, [lambda c=c: one(c) for c in companies])
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
