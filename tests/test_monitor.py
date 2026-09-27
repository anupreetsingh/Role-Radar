"""End-to-end: config → scrape (mocked HTTP) → filter → state → notify."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import httpx

import monitor
from config import AppConfig, CompanyConfig, Settings
from filters import JobFilter
from http_client import HttpClient, HttpSettings
from notifications import DiscordNotifier, Notifier, format_group, group_jobs
from storage import MemoryStateStore, MonitorState, from_iso
from tests.conftest import fixture_json, job, make_client

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
    monkeypatch.setattr(monitor, "HttpClient", lambda s: HttpClient(s, transport=transport))
    kwargs.setdefault("clock", lambda: T0)
    return asyncio.run(monitor.run(config or build_config(), store, [notifier], **kwargs))


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
    assert sum(m.count("NEW JOB") for m in messages) == 60
