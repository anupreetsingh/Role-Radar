"""Runner: holding the lease across passes (the laptop's `start` and `run --once`)."""

import asyncio

import httpx
import pytest

from role_radar import monitor
from role_radar.backends import Backend, ConfigSource
from role_radar.config import RuntimeSettings
from role_radar.http_client import HttpClient
from role_radar.runner import EXIT_LEASE_HELD, Runner
from tests.conftest import fixture_json
from tests.test_monitor import RecordingNotifier

pytest.importorskip("moto")

from role_radar.dynamo import DynamoLease, DynamoStateStore  # noqa: E402

CONFIG = """
settings:
  http: {per_domain_delay: 0, respect_robots: false, max_retries: 0}
companies:
  - name: Continental Finance
    url: https://contfinco.bamboohr.com/careers
    filters: {include_keywords: [software developer, data engineer]}
"""


@pytest.fixture
def requests_made(monkeypatch):
    made = []

    def handler(request):
        made.append(str(request.url))
        return httpx.Response(200, json=fixture_json("bamboohr_list.json"))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(monitor, "HttpClient", lambda s, **kw: HttpClient(s, transport=transport, **kw))
    return made


def make_runner(table, tmp_path, holder, notifier):
    path = tmp_path / "companies.yaml"
    path.write_text(CONFIG)
    lease = DynamoLease(table[0], table[1], holder)
    backend = Backend(DynamoStateStore(table[0], table[1], lease), lease)
    return Runner(ConfigSource(RuntimeSettings(), path), backend, [notifier])


async def until(condition, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_serve_takes_the_lease_checks_due_companies_and_releases_on_stop(table, tmp_path, requests_made):
    notifier = RecordingNotifier()
    runner = make_runner(table, tmp_path, "laptop:mac", notifier)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.serve(stop))
        await until(lambda: notifier.batches)
        assert runner.lease.held
        stop.set()
        return await task

    assert asyncio.run(go()) == 0
    (batch,) = notifier.batches
    assert len(batch) == 2
    info = runner.lease.read()
    assert info.holder == "laptop:mac" and info.released_at  # released, so Lambda can take over at once
    assert runner.store.last_runs()["laptop:mac"]["alerts"] == 2


def test_serve_waits_for_the_holder_and_asks_it_to_hand_over(table, tmp_path, requests_made):
    DynamoLease(table[0], table[1], "lambda").acquire(900)
    runner = make_runner(table, tmp_path, "laptop:mac", RecordingNotifier())

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.serve(stop))
        await until(lambda: (runner.lease.read().requested_by or "") == "laptop:mac")
        stop.set()
        return await task

    assert asyncio.run(go()) == 0
    assert requests_made == []  # never scraped without the lease
    assert runner.lease.read().holder == "lambda"


def test_run_once_refuses_while_another_runner_holds_the_lease(table, tmp_path, requests_made):
    DynamoLease(table[0], table[1], "laptop:mac").acquire(180)
    runner = make_runner(table, tmp_path, "cli:mac", RecordingNotifier())
    assert asyncio.run(runner.run_once()) == EXIT_LEASE_HELD
    assert requests_made == []


def test_run_once_releases_the_lease_afterwards(table, tmp_path, requests_made):
    notifier = RecordingNotifier()
    runner = make_runner(table, tmp_path, "cli:mac", notifier)
    assert asyncio.run(runner.run_once()) == 0
    assert len(notifier.batches) == 1
    assert DynamoLease(table[0], table[1], "lambda").acquire(900)  # free straight away
