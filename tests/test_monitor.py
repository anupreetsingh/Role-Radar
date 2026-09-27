"""End-to-end: config → scrape (mocked HTTP) → filter → state → notify."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import httpx

from role_radar import monitor
from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.filters import JobFilter
from role_radar.http_client import HttpClient, HttpSettings
from role_radar.lease import LeaseLost, LocalLease
from role_radar.notifications import DiscordNotifier, Notifier, format_digest, format_group, group_jobs
from role_radar.storage import MemoryStateStore, MonitorState, from_iso
from tests.conftest import Clock, fixture_json, job, make_client

T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self, fail=False):
        self.batches, self.fail = [], fail

    async def send(self, jobs):
        if self.fail:
            raise RuntimeError("boom")
        self.batches.append(jobs)


def build_config():
    settings = Settings(http=HttpSettings(per_domain_delay=0, respect_robots=False, max_retries=0))
    good = CompanyConfig(
        name="Continental Finance",
        url="https://contfinco.bamboohr.com/careers",
        filter=JobFilter(include_keywords=["software developer", "data engineer"], exclude_keywords=["director", "chief"]),
    )
    broken = CompanyConfig(name="Broken", url="https://broken.bamboohr.com/careers", filter=JobFilter())
    return AppConfig(settings=settings, companies=[good, broken])


def run_monitor(monkeypatch, store, notifier, list_body=None, requested=None, config=None, handler=None, **kwargs):
    list_body = list_body or fixture_json("bamboohr_list.json")

    def default_handler(request):
        if requested is not None:
            requested.append(request.url.host + request.url.path)
        if request.url.host.startswith("broken"):
            return httpx.Response(500)
        if request.url.path == "/careers/list":
            return httpx.Response(200, json=list_body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler or default_handler)
    monkeypatch.setattr(monitor, "HttpClient", lambda s, **kw: HttpClient(s, transport=transport, **kw))
    kwargs.setdefault("clock", lambda: T0)
    notifiers = notifier if isinstance(notifier, list) or callable(notifier) else [notifier]
    return asyncio.run(monitor.run(config or build_config(), store, notifiers, **kwargs))


def test_run_alerts_once_and_survives_broken_company(monkeypatch):
    store, notifier, requested = MemoryStateStore(), RecordingNotifier(), []
    assert run_monitor(monkeypatch, store, notifier, requested=requested) == 0  # Broken fails, run still succeeds
    (batch,) = notifier.batches
    assert sorted(j.title for j in batch) == ["Data Engineer", "Mid/Senior Software Developer (.NET Core / React / AWS)"]
    assert "contfinco.bamboohr.com/careers/list" in requested
    assert not any(p.endswith("/detail") for p in requested)  # listing only: no per-job detail requests

    run_monitor(monkeypatch, store, notifier, check_all=True)
    run_monitor(monkeypatch, store, notifier, clock=lambda: T0 + timedelta(hours=2))
    assert len(notifier.batches) == 1  # nothing new → no second alert


def test_failed_delivery_is_retried_at_next_check(monkeypatch):
    store = MemoryStateStore()
    assert run_monitor(monkeypatch, store, RecordingNotifier(fail=True)) == 1
    ok = RecordingNotifier()
    run_monitor(monkeypatch, store, ok, clock=lambda: T0 + timedelta(minutes=5))
    assert ok.batches == []  # not due yet
    run_monitor(monkeypatch, store, ok, clock=lambda: T0 + timedelta(hours=2))
    assert len(ok.batches) == 1 and len(ok.batches[0]) == 2


def test_only_due_companies_are_checked(monkeypatch):
    store, notifier, requested = MemoryStateStore(), RecordingNotifier(), []
    run_monitor(monkeypatch, store, notifier, requested=requested)
    assert {p.split("/")[0] for p in requested} == {"contfinco.bamboohr.com", "broken.bamboohr.com"}
    schedule = store.load_schedule()
    assert set(schedule) == {"Continental Finance", "Broken"}
    assert schedule["Broken"].failures == 1 and "HTTP 500" in schedule["Broken"].last_error

    requested.clear()
    assert run_monitor(monkeypatch, store, notifier, requested=requested, clock=lambda: T0 + timedelta(minutes=5)) == 0
    assert requested == []  # nothing due, nothing scraped

    latest = max(from_iso(m.next_check_at) for m in schedule.values())
    run_monitor(monkeypatch, store, notifier, requested=requested, clock=lambda: latest)
    assert {p.split("/")[0] for p in requested} == {"contfinco.bamboohr.com", "broken.bamboohr.com"}


def test_each_company_is_saved_as_soon_as_it_finishes(monkeypatch):
    saved: list[str] = []
    fast_saved = threading.Event()  # save_company runs in a worker thread

    class RecordingStore(MemoryStateStore):
        def save_company(self, record):
            super().save_company(record)
            saved.append(record.name)
            if record.name == "Fast":
                fast_saved.set()

    async def handler(request):
        if request.url.host.startswith("slow"):
            # Answer only once Fast's state is already in the store.
            for _ in range(500):
                if fast_saved.is_set():
                    break
                await asyncio.sleep(0.01)
            else:
                return httpx.Response(500)
        return httpx.Response(200, json=fixture_json("bamboohr_list.json"))

    companies = [
        CompanyConfig(name=name, url=f"https://{name.lower()}.bamboohr.com/careers", filter=JobFilter())
        for name in ("Slow", "Fast")
    ]
    config = AppConfig(settings=build_config().settings, companies=companies)
    assert run_monitor(monkeypatch, RecordingStore(), RecordingNotifier(), config=config, handler=handler) == 0
    assert saved == ["Fast", "Slow"]


class TakenOverLease(LocalLease):
    """Looks held from here, but the stored lease now belongs to someone else."""

    def verify(self):
        self.check()
        raise LeaseLost("lambda took over")


class ExpiringLease(LocalLease):
    def acquire(self, ttl):
        self.epoch, self.expires_at = 1, self.clock() + ttl
        return True


def test_runner_never_alerts_after_a_takeover(monkeypatch):
    store, notifier = MemoryStateStore(), RecordingNotifier()
    lease = TakenOverLease("laptop:mac")
    lease.acquire(0)
    assert run_monitor(monkeypatch, store, notifier, lease=lease) == monitor.EXIT_LEASE_LOST
    assert notifier.batches == []
    assert "Continental Finance" not in store.load_schedule()  # not saved either: still due


def test_runner_stops_before_saving_once_its_lease_expired(monkeypatch):
    clock = Clock()
    lease = ExpiringLease("laptop:mac", clock)
    lease.acquire(180)
    store, notifier = MemoryStateStore(), RecordingNotifier()

    def handler(request):
        clock.advance(3600)  # the lid closes mid-scrape; the process wakes an hour later
        return httpx.Response(200, json=fixture_json("bamboohr_list.json"))

    config = AppConfig(settings=build_config().settings, companies=build_config().companies[:1])
    assert run_monitor(monkeypatch, store, notifier, config=config, handler=handler, lease=lease) == monitor.EXIT_LEASE_LOST
    assert notifier.batches == [] and store.saves == 0


def test_baseline_checks_every_company_without_alerting(monkeypatch):
    store, notifier = MemoryStateStore(), RecordingNotifier()
    run_monitor(monkeypatch, store, notifier, baseline=True)
    run_monitor(monkeypatch, store, notifier, baseline=True, clock=lambda: T0 + timedelta(minutes=1))  # not due, still checked
    assert notifier.batches == []
    jobs = store.load_company("Continental Finance").jobs
    assert len(jobs) == 4 and sum(bool(j.notified_at) for j in jobs.values()) == 2  # matches recorded as seen


def workday_check(**filters):
    """Run check_company for a Workday board; return (outcome, requisition IDs whose detail was fetched)."""
    listing = "/wday/cxs/acme/External/jobs"
    fetched = []

    def handler(request):
        if request.url.path == listing:
            return httpx.Response(200, json=fixture_json("workday_jobs.json"))
        fetched.append(request.url.path.rsplit("_", 1)[-1])
        return httpx.Response(200, json=fixture_json("workday_detail.json"))

    async def go():
        cfg = CompanyConfig(
            name="Acme",
            url="https://acme.wd5.myworkdayjobs.com/en-US/External",
            filter=JobFilter(include_keywords=["engineer"], **filters),
        )
        async with make_client(handler) as http:
            return await monitor.check_company(cfg, http, MonitorState(), Settings(), notify=True)

    outcome = asyncio.run(go())
    return outcome, sorted(fetched)


def test_workday_details_fetched_only_when_filter_needs_them():
    outcome, fetched = workday_check()
    assert fetched == [] and len(outcome.matched) == 3

    outcome, fetched = workday_check(locations=["New York"])
    assert fetched == ["JR2000003"]  # the "2 Locations" job; the others' locations are known
    (match,) = outcome.matched
    assert match.location == "US, TX, Austin; US, NY, New York"

    outcome, fetched = workday_check(employment_types=["part time"])
    assert fetched == ["JR2000001", "JR2000002", "JR2000003"]  # the listing never has a time type
    assert outcome.matched == []


def test_notification_groups_same_title_across_locations():
    jobs = [
        job("Software Engineer, New Grad", "1", location="Austin, TX", url="https://x/1"),
        job("Software Engineer, New Grad", "2", location="Santa Clara, CA", url="https://x/2"),
    ]
    (group,) = group_jobs(jobs)
    text = format_group(group)
    assert text.startswith("NEW JOB\n\nCompany: Acme\nTitle: Software Engineer, New Grad")
    assert "Locations: Austin, TX | Santa Clara, CA" in text
    assert "Austin, TX: https://x/1" in text


def test_discord_messages_respect_length_limit():
    jobs = [job(f"Software Engineer {i}", str(i), url=f"https://acme.example/jobs/{i}") for i in range(60)]
    messages = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert all(len(m) <= 2000 for m in messages)
    assert len(messages) == 1 and all(j.url in format_digest(jobs) for j in jobs)


class FlakyStore(MemoryStateStore):
    """Saves fail while `failing` is true (e.g. DynamoDB throttling that outlasts the retries)."""

    def __init__(self):
        super().__init__()
        self.failing = False
        self.attempts = 0

    def save_company(self, record):
        self.attempts += 1
        if self.failing:
            raise RuntimeError("ThrottlingException")
        super().save_company(record)


def test_alerts_sent_before_a_failed_save_are_not_sent_again(monkeypatch):
    monkeypatch.setattr(monitor, "SAVE_RETRIES_AFTER_ALERTS", 1)
    monkeypatch.setattr(monitor, "UNSAVED_HOLD", 0.2)
    monkeypatch.setattr(monitor, "SAVE_RETRY_BASE", 0)
    store, notifier, unsaved = FlakyStore(), RecordingNotifier(), monitor.Unsaved()
    config = AppConfig(settings=build_config().settings, companies=build_config().companies[:1])

    store.failing = True
    run_monitor(monkeypatch, store, notifier, config=config, unsaved=unsaved)
    assert len(notifier.batches) == 1 and store.attempts == 2  # sent, then the save failed twice
    assert unsaved.holding("Continental Finance")
    run_monitor(monkeypatch, store, notifier, config=config, unsaved=unsaved)
    assert store.attempts == 2  # held: not even re-checked yet

    import time as _time

    _time.sleep(0.25)
    store.failing = False
    run_monitor(monkeypatch, store, notifier, config=config, unsaved=unsaved)  # still due: checked again
    assert len(notifier.batches) == 1  # ...but the same alerts weren't sent twice
    jobs = store.load_company("Continental Finance").jobs
    assert sum(bool(j.notified_at) for j in jobs.values()) == 2 and not unsaved.sent


def test_first_run_rule_survives_a_failed_first_check(monkeypatch):
    settings = Settings(notify_on_first_run=False, http=HttpSettings(per_domain_delay=0, respect_robots=False, max_retries=0))
    company = CompanyConfig(
        name="Continental Finance",
        url="https://contfinco.bamboohr.com/careers",
        filter=JobFilter(include_keywords=["software developer", "data engineer"]),
    )
    config = AppConfig(settings=settings, companies=[company])
    store, notifier, status = MemoryStateStore(), RecordingNotifier(), {"code": 503}

    def handler(request):
        return httpx.Response(status["code"], json=fixture_json("bamboohr_list.json"))

    run_monitor(monkeypatch, store, notifier, config=config, handler=handler)  # first check fails
    status["code"] = 200
    run_monitor(monkeypatch, store, notifier, config=config, handler=handler, check_all=True)
    assert notifier.batches == []  # still its first successful check: recorded silently


def test_partial_delivery_survives_failed_state_save(monkeypatch):
    monkeypatch.setattr(monitor, "SAVE_RETRIES_AFTER_ALERTS", 1)
    monkeypatch.setattr(monitor, "SAVE_RETRY_BASE", 0)
    monkeypatch.setattr(monitor, "UNSAVED_HOLD", 0)
    store, unsaved = FlakyStore(), monitor.Unsaved()
    discord, email = RecordingNotifier(), RecordingNotifier(fail=True)
    discord.name, email.name = "discord", "email"
    store.failing = True
    assert run_monitor(monkeypatch, store, [discord, email], unsaved=unsaved) == 1
    assert len(discord.batches) == 1 and unsaved.channels
    store.failing, email.fail = False, False
    assert run_monitor(monkeypatch, store, [discord, email], unsaved=unsaved) == 0
    assert len(discord.batches) == len(email.batches) == 1
    assert not unsaved.channels


def test_secret_loading_failure_saves_pending_jobs_for_retry(monkeypatch):
    class MissingSecrets:
        def __call__(self):
            raise RuntimeError("SSM unavailable")

    store = MemoryStateStore()
    assert run_monitor(monkeypatch, store, MissingSecrets()) == 1
    record = store.load_company("Continental Finance")
    assert record.meta.next_check_at and any(j.matched and not j.notified_at for j in record.jobs.values())
    notifier = RecordingNotifier()
    assert run_monitor(monkeypatch, store, notifier, check_all=True) == 0
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 2


def test_bounded_gates_limit_their_jobs_without_holding_other_jobs_slots():
    from role_radar.monitor import _bounded

    running, peak, order = [0], [0], []

    def job(name, seconds):
        async def run():
            running[0] += 1
            peak[0] = max(peak[0], running[0]) if name.startswith("wd") else peak[0]
            await asyncio.sleep(seconds)
            running[0] -= 1
            order.append(name)
            return name
        return run

    async def go():
        gate = asyncio.Semaphore(1)
        jobs = [job(f"wd{i}", 0.05) for i in range(4)] + [job("gh", 0)]
        return await _bounded(2, jobs, [gate] * 4 + [None])

    assert asyncio.run(go()) == ["wd0", "wd1", "wd2", "wd3", "gh"]
    assert peak[0] == 1  # one gated job at a time
    assert order[0] == "gh"  # the ungated job didn't queue behind gated ones


def test_max_alert_age_days_records_old_new_matches_without_alerting():
    from datetime import date, datetime, timezone

    from role_radar.storage import MonitorState
    from role_radar.tracker import reconcile

    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    fresh = job("Software Engineer", job_id="1", date_posted=date(2026, 9, 20))
    old = job("Data Engineer", job_id="2", date_posted=date(2025, 6, 10))
    undated = job("QA Engineer", job_id="3")
    state = MonitorState()
    diff = reconcile(state, "Acme", [fresh, old, undated], {j.uid: True for j in (fresh, old, undated)},
                     max_alert_age_days=14, now=now)
    assert [j.job_id for j in diff.to_notify] == ["1", "3"]  # no date: can't tell, so it alerts
    assert [(j.job_id, reason) for j, reason in diff.suppressed] == [("2", "posted 474 days ago")]
    assert state.companies["Acme"][old.uid].notified_at  # recorded, never alerts later
