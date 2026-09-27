"""Quick checks: between full checks, read a newest-first Workday board's top page only."""

import json
from datetime import timedelta

import httpx
import pytest

from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.filters import JobFilter
from role_radar.http_client import HttpSettings
from role_radar.storage import JsonStateStore, from_iso
from tests.test_digest import spy_loads
from tests.test_monitor import RecordingNotifier, T0, run_monitor

URL = "https://acme.wd5.myworkdayjobs.com/External"


class Board:
    """A Workday listing, newest job first, that counts the pages it serves."""

    def __init__(self, count):
        self.ids = [f"JR{n}" for n in range(count, 0, -1)]
        self.next_id = count + 1
        self.offsets = []

    def post(self, count=1):
        for _ in range(count):
            self.ids.insert(0, f"JR{self.next_id}")
            self.next_id += 1

    def handler(self, request):
        body = json.loads(request.content)
        self.offsets.append(body["offset"])
        page = self.ids[body["offset"] : body["offset"] + body["limit"]]
        postings = [{"title": f"Software Engineer {i}", "externalPath": f"/job/Austin/Software-Engineer_{i}",
                     "locationsText": "Austin, TX", "postedOn": "Posted Today"} for i in page]
        return httpx.Response(200, json={"total": len(self.ids), "jobPostings": postings})


def config(**settings):
    defaults = dict(check_interval_by_ats={"workday": 240}, quick_check_by_ats={"workday": 10},
                    notify_on_first_run=False, http=HttpSettings(per_domain_delay=0, respect_robots=False, max_retries=0))
    return AppConfig(settings=Settings(**{**defaults, **settings}),
                     companies=[CompanyConfig(name="Acme", url=URL, filter=JobFilter(include_keywords=["engineer"]))])


@pytest.fixture(params=["json", "dynamodb"])
def store_factory(request, tmp_path):
    if request.param == "json":
        return lambda: JsonStateStore(tmp_path / "state.json")
    from role_radar.dynamo import DynamoStateStore

    client, table = request.getfixturevalue("table")
    return lambda: DynamoStateStore(client, table)


def run(monkeypatch, store, board, notifier, minutes, **kwargs):
    board.offsets.clear()
    return run_monitor(monkeypatch, store, notifier, config=kwargs.pop("cfg", None) or config(), handler=board.handler,
                       clock=lambda: T0 + timedelta(minutes=minutes), **kwargs)


def alerted(notifier):
    return sorted(j.title.rsplit(" ", 1)[1] for batch in notifier.batches for j in batch)


def test_quick_check_reads_one_page_and_no_state_when_nothing_is_new(monkeypatch, store_factory):
    board, notifier = Board(45), RecordingNotifier()
    run(monkeypatch, store_factory(), board, notifier, 0)
    assert board.offsets == [0, 20, 40]  # the first check is a full one
    full_due = store_factory().load_schedule()["Acme"].next_check_at

    run(monkeypatch, store_factory(), board, notifier, 5)
    assert board.offsets == []  # not due yet

    loaded = []
    assert run(monkeypatch, spy_loads(store_factory(), loaded), board, notifier, 10) == 0
    assert board.offsets == [0] and loaded == [] and notifier.batches == []
    meta = store_factory().load_schedule()["Acme"]
    assert from_iso(meta.next_quick_at) == T0 + timedelta(minutes=20)
    assert meta.next_check_at == full_due  # the full check's schedule is untouched


def test_quick_check_alerts_on_new_jobs_reading_only_until_a_known_one(monkeypatch, store_factory):
    board, notifier = Board(45), RecordingNotifier()
    run(monkeypatch, store_factory(), board, notifier, 0)

    board.post()
    run(monkeypatch, store_factory(), board, notifier, 10)
    assert board.offsets == [0] and alerted(notifier) == ["JR46"]

    board.post(25)  # more than a page: the second page still starts with new jobs
    run(monkeypatch, store_factory(), board, notifier, 20)
    assert board.offsets == [0, 20]
    assert alerted(notifier) == sorted(["JR46"] + [f"JR{n}" for n in range(47, 72)])

    run(monkeypatch, store_factory(), board, notifier, 30)
    assert board.offsets == [0] and len(notifier.batches) == 2  # nothing new since


def test_quick_check_never_marks_jobs_removed_but_the_full_check_does(monkeypatch, store_factory):
    board, notifier = Board(45), RecordingNotifier()
    run(monkeypatch, store_factory(), board, notifier, 0)
    board.ids.remove("JR44")  # page 1 now shows JR25, a known job, so the company is read
    board.ids.remove("JR1")  # off the first page: only a full check can see it's gone

    run(monkeypatch, store_factory(), board, notifier, 10)
    assert board.offsets == [0] and notifier.batches == []
    jobs = store_factory().load_company("Acme").jobs
    assert not any(j.removed_at for j in jobs.values())

    run(monkeypatch, store_factory(), board, notifier, 241 + 240)  # the full check, however late
    jobs = store_factory().load_company("Acme").jobs
    assert sorted(uid.rsplit(":", 1)[1] for uid, j in jobs.items() if j.removed_at) == ["JR1", "JR44"]


def test_quick_check_stops_after_its_page_cap(monkeypatch):
    from role_radar.scrapers import workday
    from role_radar.storage import MemoryStateStore

    board, notifier, store = Board(10), RecordingNotifier(), MemoryStateStore()
    run(monkeypatch, store, board, notifier, 0)
    board.post(workday.QUICK_MAX_PAGES * 20 + 30)
    run(monkeypatch, store, board, notifier, 10)
    assert board.offsets == [n * 20 for n in range(workday.QUICK_MAX_PAGES)]


def test_only_quick_check_ats_get_quick_checks(monkeypatch):
    from role_radar.storage import MemoryStateStore

    board, notifier, store = Board(5), RecordingNotifier(), MemoryStateStore()
    cfg = config(quick_check_by_ats={})
    run(monkeypatch, store, board, notifier, 0, cfg=cfg)
    assert store.state.meta["Acme"].top_uids is None and store.state.meta["Acme"].next_quick_at is None
    run(monkeypatch, store, board, notifier, 30, cfg=cfg)
    assert board.offsets == []


def test_quick_check_failure_waits_for_the_next_quick_check(monkeypatch):
    from role_radar.storage import MemoryStateStore

    board, notifier, store = Board(5), RecordingNotifier(), MemoryStateStore()
    run(monkeypatch, store, board, notifier, 0)
    full_due = store.state.meta["Acme"].next_check_at
    board.handler = lambda request: httpx.Response(503)
    run(monkeypatch, store, board, notifier, 10)
    meta = store.state.meta["Acme"]
    assert from_iso(meta.next_quick_at) == T0 + timedelta(minutes=20)
    assert meta.next_check_at == full_due and meta.failures == 0


@pytest.mark.parametrize("minutes", [0, -5, float("inf"), True, "10"])
def test_invalid_quick_check_interval_is_rejected(minutes):
    with pytest.raises(ValueError, match="quick_check_by_ats"):
        Settings(quick_check_by_ats={"workday": minutes})
