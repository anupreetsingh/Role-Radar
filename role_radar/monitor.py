#!/usr/bin/env python3
"""Role Radar: check careers pages and alert on newly posted matching jobs.

Flow of one run:
  load config + every company's schedule → pick the companies that are due
  → for each due company (concurrently, bounded):
      load its state → scrape listing → fetch details only for unseen jobs whose
      filter needs a field the listing lacks (and that could still match)
      → filter → diff against state
      → confirm the lease is still ours → alert on its new matches
      → save its state and next check time straight away (fenced on the lease)

If the lease turns out to be lost, the run stops without saving or sending
anything more. The companies it didn't finish are still due, so whoever holds
the lease picks them up.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Awaitable, Callable, Iterable, Sequence, TypeVar, Union

from role_radar.backends import ConfigSource, NotifierSource, open_backend, resolve_runtime
from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.http_client import HttpClient
from role_radar.lease import Lease, LeaseLost, LocalLease
from role_radar.models import JobPosting
from role_radar.notifications import ConsoleNotifier, Notifier, notify_all
from role_radar.schedule import after_check, due_companies, next_due
from role_radar.scrapers import BaseScraper, scraper_class_for
from role_radar.storage import MonitorState, SeenJob, StateStore, to_iso, utcnow
from role_radar.tracker import CompanyDiff, dedupe, mark_notified, reconcile

log = logging.getLogger("monitor")

T = TypeVar("T")
# A list of channels, or a function that builds them the first time they're needed.
Notifiers = Union[Sequence[Notifier], Callable[[], Sequence[Notifier]]]

EXIT_LEASE_LOST = 3


@dataclass
class CompanyOutcome:
    company: str
    error: str | None = None
    listed: int = 0
    matched: list[JobPosting] = field(default_factory=list)
    diff: CompanyDiff | None = None
    delivered: bool = True  # False when there were alerts and no channel took them

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def alerted(self) -> int:
        return len(self.diff.to_notify) if self.diff and self.delivered else 0


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
    log.info("[%s] %d job(s) listed via %s", company.name, len(jobs), scraper.name)

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
    log.info(
        "[%s] matched=%d new=%d alert=%d removed=%d returned=%d suppressed=%d",
        company.name, len(matched), len(diff.new), len(diff.to_notify),
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
            timeout=settings.company_timeout,
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
    record.meta = after_check(record.meta, now, settings.check_interval, error=outcome.error)
    if not dry_run:
        lease.check()  # fail fast; the store also checks the stored lease in the same transaction
        await asyncio.to_thread(store.save_company, record)
    return outcome


async def run(
    config: AppConfig,
    store: StateStore,
    notifiers: Notifiers,
    *,
    lease: Lease | None = None,
    dry_run: bool = False,
    baseline: bool = False,
    only: set[str] | None = None,
    list_matches: bool = False,
    check_all: bool = False,
    clock: Callable[[], datetime] = utcnow,
) -> int:
    """One pass over the companies that are due (all of them with check_all, baseline or only).

    The caller must already hold `lease` (a LocalLease is used when none is given).
    Returns 0 on success, 1 if every company failed or an alert couldn't be
    delivered, 2 if there was nothing configured, 3 if the lease was lost.
    """
    settings = config.settings
    companies = [c for c in config.companies if c.enabled and (not only or c.name.lower() in only)]
    if not companies:
        log.error("No companies to check")
        return 2
    if list_matches:
        return await print_matches(companies, settings)
    if lease is None:
        lease = LocalLease("local")
        lease.acquire(0)

    if not (check_all or baseline or only):
        schedule = await asyncio.to_thread(store.load_schedule)
        now = clock()
        due = due_companies(companies, schedule, now, settings.check_interval)
        log.info("%d of %d companies due", len(due), len(companies))
        if not due:
            upcoming = next_due(companies, schedule, now, settings.check_interval)
            log.info("Nothing to do; next check due at %s", to_iso(upcoming) if upcoming else "-")
            return 0
        companies = due

    started = time.monotonic()
    if dry_run:
        channels: Callable[[], Sequence[Notifier]] = lambda: [ConsoleNotifier()]  # noqa: E731
    else:
        channels = notifiers if callable(notifiers) else (lambda: notifiers)

    async def guarded(company: CompanyConfig) -> CompanyOutcome:
        try:
            return await process_company(
                company, http, store, channels, settings, now=clock(), lease=lease, notify=not baseline, dry_run=dry_run
            )
        except LeaseLost:
            raise
        except Exception as exc:  # loading or saving state failed; the company stays due
            msg = str(exc) or type(exc).__name__
            log.error("[%s] state could not be loaded or saved: %s", company.name, msg)
            return CompanyOutcome(company.name, error=msg)

    async with HttpClient(settings.http) as http:
        try:
            outcomes = await _bounded(settings.max_company_concurrency, [lambda c=c: guarded(c) for c in companies])
        except LeaseLost as exc:
            log.error("Stopped: %s. Unfinished companies are still due for whoever holds the lease.", exc)
            return EXIT_LEASE_LOST

    log.info("Checked %d companies in %.1fs: %s", len(companies), time.monotonic() - started, http.stats.summary())
    for host, stats in http.stats.busiest():
        log.debug("  %s: %d requests, %d bytes, %d failed", host, stats.requests, stats.bytes, stats.errors)
    if dry_run:
        log.info("Dry run: state not saved")

    failed = [o for o in outcomes if not o.ok]
    undelivered = [o for o in outcomes if not o.delivered]
    alerted = sum(o.alerted for o in outcomes)
    log.info(
        "Done: %d/%d companies ok, %d new alert(s)%s",
        len(outcomes) - len(failed), len(outcomes), alerted,
        f"; failed: {', '.join(o.company for o in failed)}" if failed else "",
    )  # fmt: skip
    write_step_summary(outcomes, alerted)

    if len(failed) == len(outcomes) or undelivered:
        return 1
    return 0


async def print_matches(companies: list[CompanyConfig], settings: Settings) -> int:
    """Print every job matching right now. Reads no state, sends nothing, saves nothing."""

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
    return 0


def write_step_summary(outcomes: list[CompanyOutcome], alerted: int) -> None:
    """Append a Markdown table to the GitHub Actions job summary, if running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = ["| Company | Status | Listed | Matched | New alerts | Removed |", "|---|---|---|---|---|---|"]
    for o in outcomes:
        status = "ok" if o.ok else f"error: {(o.error or '')[:80]}"
        d = o.diff
        rows.append(f"| {o.company} | {status} | {o.listed} | {len(o.matched)} | {o.alerted} | {len(d.removed) if d else 0} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"### Role Radar: {alerted} alert(s) sent\n\n" + "\n".join(rows) + "\n")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request URL at INFO, which would include webhook URLs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/companies.yaml", help="companies YAML/JSON file (its runtime: section picks the backends)")
    p.add_argument("--state", help="use this JSON state file (overrides runtime.storage)")
    p.add_argument("--company", action="append", default=[], help="only check this company, due or not (repeatable)")
    p.add_argument("--all", action="store_true", help="check every company, not just the ones that are due")
    p.add_argument("--dry-run", action="store_true", help="print alerts to stdout and don't save state")
    p.add_argument("--baseline", action="store_true", help="record every company's current jobs as seen without alerting")
    p.add_argument("--list-matches", action="store_true", help="print every currently matching job and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (shows why each job matched or not)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        runtime = resolve_runtime(args.config)
        if args.state:
            runtime = replace(runtime, storage="json", state_file=args.state)
        config = ConfigSource(runtime, args.config).load()
    except (OSError, ValueError, RuntimeError) as exc:
        log.error("Invalid config: %s", exc)
        return 2
    backend = open_backend(runtime, f"cli:{socket.gethostname()}")
    needs_lease = not (args.dry_run or args.list_matches)
    if needs_lease and not backend.lease.acquire(ttl=15 * 60):
        info = backend.lease.read()
        log.error("%s holds the lease; not running (it expires at %s)", info.holder if info else "?", info and time.ctime(info.expires_at))
        return 4
    try:
        return asyncio.run(
            run(
                config,
                backend.store,
                NotifierSource(runtime),
                lease=backend.lease if needs_lease else None,
                dry_run=args.dry_run,
                baseline=args.baseline,
                only={c.lower() for c in args.company} or None,
                list_matches=args.list_matches,
                check_all=args.all,
            )
        )
    finally:
        if needs_lease:
            backend.lease.release()


if __name__ == "__main__":
    sys.exit(main())
