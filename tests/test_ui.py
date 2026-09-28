"""`role-radar ui`: the local runner-switch page and its API."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from role_radar import ui
from role_radar.backends import Backend
from role_radar.lease import LocalLease
from role_radar.storage import MemoryStateStore, MonitorState


@pytest.fixture
def served():
    store = MemoryStateStore()
    server = ui.serve(Backend(store, LocalLease("local")), lambda: 4242, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", store
    server.shutdown()
    server.server_close()


def call(url, body=None, header=True):
    headers = {"Content-Type": "application/json", **({ui.WRITE_HEADER: "1"} if header else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_page_and_state(served):
    base, _ = served
    with urllib.request.urlopen(base + "/") as resp:
        assert b"Role Radar runners" in resp.read()
    status, state = call(base + "/api/state")
    assert state["switches"] == {"laptop": True, "lambda": True, "discord": True, "email": True}
    assert status == 200 and state["laptop_app_pid"] == 4242


def test_switching_a_runner(served):
    base, store = served
    status, state = call(base + "/api/switch", {"runner": "lambda", "on": False})
    assert status == 200 and state["switches"] == {"laptop": True, "lambda": False, "discord": True, "email": True}
    assert store.load_switches() == {"lambda": False}


def test_rejects_bad_or_cross_site_writes(served):
    base, store = served
    assert call(base + "/api/switch", {"runner": "lambda", "on": False}, header=False)[0] == 403  # no custom header
    assert call(base + "/api/switch", {"runner": "server", "on": False})[0] == 400
    assert call(base + "/api/switch", {"runner": "laptop", "on": "no"})[0] == 400
    assert store.load_switches() == {}


def test_switches_survive_the_json_state_file():
    state = MonitorState(switches={"laptop": False})
    assert MonitorState.from_dict(state.to_dict()).switches == {"laptop": False}
    assert "switches" not in MonitorState().to_dict()


@pytest.mark.parametrize("switches, holder, pid, expected", [
    ({"laptop": True, "lambda": True}, None, 123, "laptop"),  # both on: the Mac while its app runs
    ({"laptop": True, "lambda": True}, None, None, "lambda"),  # ...and Lambda when it doesn't
    ({"laptop": True, "lambda": True}, "lambda:abc", 123, "lambda"),  # Lambda mid-run until the Mac takes over
    ({"laptop": False, "lambda": True}, None, 123, "lambda"),
    ({"laptop": True, "lambda": False}, None, 123, "laptop"),
    ({"laptop": True, "lambda": False}, None, None, None),  # Mac on but its app isn't running
    ({"laptop": False, "lambda": False}, None, 123, None),
])
def test_who_is_checking(switches, holder, pid, expected):
    assert ui.checking(switches, holder, pid) == expected


def test_activity_fills_every_hour_and_counts_the_mac_and_lambda_apart():
    from datetime import datetime, timezone

    store = MemoryStateStore()
    store.record_stats("laptop", "2026-09-27T09", {"checked": 99})  # 25 hours ago: too old
    store.record_stats("laptop", "2026-09-28T08", {"checked": 5, "new_jobs": 3, "matches": 1, "alerts": 1})
    store.record_stats("cli", "2026-09-28T08", {"checked": 1})
    store.record_stats("lambda", "2026-09-28T09", {"checked": 7, "failed": 2})
    day = ui.activity(store, datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc))
    assert len(day["hours"]) == 24
    assert day["hours"][0] == {"start": "2026-09-27T10:00:00Z", "mac": 0, "lambda": 0}
    assert day["hours"][-2:] == [{"start": "2026-09-28T08:00:00Z", "mac": 6, "lambda": 0},
                                 {"start": "2026-09-28T09:00:00Z", "mac": 0, "lambda": 7}]
    assert {k: day[k] for k in ("checked", "failed", "new_jobs", "matches", "alerts")} == {
        "checked": 13, "failed": 2, "new_jobs": 3, "matches": 1, "alerts": 1}


def test_stats_survive_the_json_state_file_and_old_hours_are_dropped(tmp_path):
    from role_radar.storage import JsonStateStore

    store = JsonStateStore(tmp_path / "state.json")
    store.record_stats("laptop", "2026-09-25T09", {"checked": 1})
    store.record_stats("laptop", "2026-09-28T09", {"checked": 2})
    assert JsonStateStore(tmp_path / "state.json").load_stats("2000-01-01T00") == [
        {"hour": "2026-09-28T09", "runner": "laptop", "checked": 2}]


def test_snapshot_shows_the_latest_pass_and_whether_alerts_are_off(served):
    base, store = served
    store.record_run("lambda", {"finished_at": "2026-09-28T09:00:00Z", "checked": 3, "pending": 5, "failing": 1})
    store.record_run("laptop:mac", {"finished_at": "2026-09-28T09:10:00Z", "checked": 40, "failed": 0, "pending": 4})
    store.save_switch("discord", False)
    store.save_switch("email", False)
    status, state = call(base + "/api/state")
    assert status == 200 and state["alerts_off"] is True
    assert state["latest_pass"] == {"runner": "laptop", "finished_at": "2026-09-28T09:10:00Z", "checked": 40,
                                    "failed": 0, "seconds": None, "failing": None, "pending": 4}
    assert len(state["activity"]["hours"]) == 24 and "next_digest" in state
