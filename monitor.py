#!/usr/bin/env python3
"""Role Radar: check careers pages and alert on newly posted matching jobs.

Flow of one run:
  load config + state
  → for each company (concurrently, bounded): scrape listing
    → fetch details only for unseen jobs whose filter needs a field the listing
      lacks (and that could still match) → filter → diff against state
  → send one batched notification for all new matches
  → mark delivered jobs as notified → save state
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig, CompanyConfig, Settings, load_config
from http_client import HttpClient
from models import JobPosting
from notifications import ConsoleNotifier, Notifier, notifiers_from_env, notify_all
from scrapers import BaseScraper, scraper_class_for
from storage import JsonStateStore, MonitorState, SeenJob, StateStore
from tracker import CompanyDiff, dedupe, mark_notified, reconcile

log = logging.getLogger("monitor")


@dataclass
class CompanyOutcome:
    company: str
    error: str | None = None
    listed: int = 0
    matched: list[JobPosting] = field(default_factory=list)
    diff: CompanyDiff | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _needs_details(rec: SeenJob | None) -> bool:
    return rec is None or not (rec.notified_at or rec.duplicate_of or rec.detail_fetched)


async def _fetch_details(scraper: BaseScraper, job: JobPosting) -> bool:
    try:
        await scraper.fetch_details(job)
        return True
    except Exception as exc:
        log.warning("[%s] details failed for %r: %s", job.company, job.title, exc)
        return False


async def check_company(
    company: CompanyConfig,
    http: HttpClient,
    state: MonitorState,
    settings: Settings,
    *,
    notify: bool,
) -> CompanyOutcome:
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


async def run(
    config: AppConfig,
    store: StateStore,
    notifiers: list[Notifier],
    *,
    dry_run: bool = False,
    baseline: bool = False,
    only: set[str] | None = None,
    list_matches: bool = False,
) -> int:
    settings = config.settings
    state = store.load()
    companies = [c for c in config.companies if c.enabled and (not only or c.name.lower() in only)]
    if not companies:
        log.error("No companies to check")
        return 2

    sem = asyncio.Semaphore(settings.max_company_concurrency)

    async with HttpClient(settings.http) as http:

        async def guarded(company: CompanyConfig) -> CompanyOutcome:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        check_company(company, http, state, settings, notify=not baseline),
                        timeout=settings.company_timeout,
                    )
                except Exception as exc:  # one broken company must not stop the run
                    msg = str(exc) or type(exc).__name__
                    log.error("[%s] failed: %s", company.name, msg)
                    return CompanyOutcome(company.name, error=msg)

        outcomes = await asyncio.gather(*(guarded(c) for c in companies))

    if list_matches:
        for o in outcomes:
            for job in o.matched:
                print(f"{o.company} | {job.title} | {job.location or '-'} | {job.url}")
        return 0

    to_notify = [j for o in outcomes if o.diff for j in o.diff.to_notify]
    delivered = True
    if to_notify:
        delivered = await notify_all([ConsoleNotifier()] if dry_run else notifiers, to_notify)
        if delivered:
            mark_notified(state, to_notify)
        else:
            log.error("No notification channel succeeded; %d job(s) will be retried next run", len(to_notify))

    pruned = state.prune(settings.retention_days)
    if pruned:
        log.info("Pruned %d long-removed job(s) from state", pruned)
    if dry_run:
        log.info("Dry run: state not saved")
    else:
        store.save(state)

    failed = [o for o in outcomes if not o.ok]
    log.info(
        "Done: %d/%d companies ok, %d new alert(s)%s",
        len(outcomes) - len(failed), len(outcomes), len(to_notify),
        f"; failed: {', '.join(o.company for o in failed)}" if failed else "",
    )  # fmt: skip
    write_step_summary(outcomes, len(to_notify) if delivered else 0)

    if len(failed) == len(outcomes) or not delivered:
        return 1
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
        rows.append(
            f"| {o.company} | {status} | {o.listed} | {len(o.matched)} | "
            f"{len(d.to_notify) if d else 0} | {len(d.removed) if d else 0} |"
        )
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
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(root / "config" / "companies.yaml"), help="companies YAML/JSON file")
    p.add_argument("--state", help="state file (default: settings.state_file, relative to the project)")
    p.add_argument("--company", action="append", default=[], help="only check this company (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="print alerts to stdout and don't save state")
    p.add_argument("--baseline", action="store_true", help="record current jobs as seen without alerting")
    p.add_argument("--list-matches", action="store_true", help="print every currently matching job and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (shows why each job matched or not)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        log.error("Invalid config: %s", exc)
        return 2
    if args.state:
        state_path = Path(args.state)
    else:
        state_path = Path(config.settings.state_file)
        if not state_path.is_absolute():
            state_path = Path(__file__).resolve().parent / state_path
    return asyncio.run(
        run(
            config,
            JsonStateStore(state_path),
            notifiers_from_env(),
            dry_run=args.dry_run or args.list_matches,
            baseline=args.baseline,
            only={c.lower() for c in args.company} or None,
            list_matches=args.list_matches,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
