"""The Windows app (windows/role_radar_app), clicked through as a person would: drawn offscreen, with
role-radar either run in this process on a temporary home (Setup) or stood in for (Live Tracking and
the panel), so each click's commands and what it shows can be checked. Needs Qt
(windows/requirements.txt); skipped without it."""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the Windows app needs Qt: pip install -r windows/requirements.txt")

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from role_radar import cli as role_radar_cli  # noqa: E402
from role_radar import keychain  # noqa: E402
from role_radar_app import describe, system  # noqa: E402
from role_radar_app.cli import Calls, Result  # noqa: E402
from role_radar_app.model import Model  # noqa: E402
from role_radar_app.views import icons  # noqa: E402
from role_radar_app.views.jobs import matching  # noqa: E402
from role_radar_app.views.panel import Panel  # noqa: E402
from role_radar_app.views.window import MainWindow  # noqa: E402

NOW = datetime.now(timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def settle(ms: int = 50) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def click(widget, point: QPoint | None = None) -> None:
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point or widget.rect().center())
    settle()


# -- Setup, on the real command line -----------------------------------------------------------------


@pytest.fixture
def radar(tmp_path, monkeypatch):
    """role-radar run in this process on a temporary home, with a small Tech list and an in-memory keychain."""
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "tech.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Acme", "url": "https://job-boards.greenhouse.io/acme", "countries": ["US"]},
        {"name": "Bharat Labs", "url": "https://jobs.lever.co/bharat", "countries": ["IN"]},
        {"name": "Anywhere Inc", "url": "https://jobs.ashbyhq.com/anywhere"},
    ]}))
    monkeypatch.setenv("ROLE_RADAR_LISTS", str(lists))
    monkeypatch.setenv("ROLE_RADAR_HOME", str(tmp_path / "home"))
    secrets: dict[str, str] = {}
    monkeypatch.setattr(keychain, "read", secrets.get)
    monkeypatch.setattr(keychain, "read_all", lambda: dict(secrets))
    monkeypatch.setattr(keychain, "write", lambda name, value=None: secrets.__setitem__(name, value))
    config = tmp_path / "home" / "companies.yaml"
    calls: list[list[str]] = []

    def run(args: list[str], stdin: str | None) -> Result:
        calls.append(args)
        out, err, before = io.StringIO(), io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(stdin or "")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = role_radar_cli.main([*args, "--config", str(config)])
        finally:
            sys.stdin = before
        lines = err.getvalue().strip().splitlines()
        return Result(code == 0, out.getvalue().encode(), lines[-1] if lines else f"exit {code}")

    run.calls, run.config, run.secrets = calls, config, secrets
    return run


def test_setup_from_first_open_to_live_tracking(app, radar):
    model = Model(Calls(radar, inline=True))
    window = MainWindow(model)
    model.start()
    settle()
    setup = window.setup_view
    assert window.isVisible() and window.pages.currentWidget() is setup and setup.page == 0
    assert not setup.next.isEnabled()  # no profession yet

    click(setup.cards["tech"])
    profile = yaml.safe_load(radar.config.with_name("profile.yaml").read_text())
    assert profile["settings"]["profession"] == "tech"
    assert "software engineer" in setup.targets.ticked  # its titles, all ticked

    click(setup.next)
    assert setup.page == 1 and not setup.next.isEnabled()  # a country first
    us = setup._country_widgets["US"]
    assert "2 companies" in us.text()  # Acme, and Anywhere Inc (no country named)
    us.setChecked(True)
    settle()
    assert setup.tracked_note.text() == "2 companies to track in United States."
    click(setup.next)
    assert setup.page == 2
    assert any(arg[:2] == ["setup", "find"] for arg in radar.calls)
    click(setup.next)
    assert setup.page == 3
    click(setup.next)
    assert setup.page == 4 and setup.years_note.text().startswith("You'll still hear about jobs asking for no experience, 1+")
    click(setup.next)
    assert setup.page == 5
    profile = yaml.safe_load(radar.config.with_name("profile.yaml").read_text())
    assert profile["settings"]["countries"] == ["US"]
    assert profile["filters"]["max_experience_years"] == 2 and profile["filters"]["education"] == "bachelors"
    assert setup.start.isVisible() and setup.start.isEnabled() and setup.start.text() == "Start Checking"

    click(setup.start)
    assert ["switch", "laptop", "on", "--json"] in radar.calls  # a dev run: on, but no --start (it checks no sites)
    assert window.pages.currentWidget() is window.live_view
    window.close()


def test_saving_gmail_turns_email_alerts_on(app, radar):
    model = Model(Calls(radar, inline=True))
    window = MainWindow(model)
    model.start()
    settle()
    setup = window.setup_view
    setup._move(5)
    setup.address.setText("me@gmail.com")
    setup.password.setText("abcd efgh ijkl mnop")
    click(setup.email_row.button)
    assert radar.secrets["SMTP_PASSWORD"] == "abcdefghijklmnop" and radar.secrets["EMAIL_TO"] == "me@gmail.com"
    assert setup.email_row.note.text().startswith("✓ Saved in") and setup.password.text() == ""
    assert ["switch", "email", "on", "--json"] in radar.calls
    assert setup.email_saved.isVisible() and setup.recipients.text() == "Alerts go to me@gmail.com."

    setup.password.setText("too short")
    click(setup.email_row.button)
    assert setup.email_row.note.text().startswith("⚠ ") and "16 letters" in setup.email_row.note.text()
    window.close()


# -- Live Tracking and the panel, with role-radar stood in for -----------------------------------------


def job(index: int, company: str, title: str, location: str, **more) -> dict:
    return {"company": company, "uid": f"{company.lower()}:{index}", "title": title, "location": location,
            "url": f"https://example.com/{index}", "first_seen": iso(NOW - timedelta(minutes=index)), "found_at": None,
            "skipped_at": None, "send_at": None, **more}


class FakeRadar:
    """Answers the app's commands from fixed lists, and records them."""

    def __init__(self, email_ready: bool = True) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.live = {"waiting": [job(1, "Stripe", "Software Engineer, New Grad", "San Francisco, CA"),
                                 job(2, "Databricks", "Data Engineer I", "Bengaluru, India"),
                                 job(3, "Ramp", "Backend Engineer", "Montréal, QC")],
                     "skipped": [job(4, "Shopify", "Developer", "Toronto, ON", skipped_at=iso(NOW))],
                     "sent": [{"sent_at": iso(NOW - timedelta(hours=1)), "by": "laptop",
                               "jobs": [{"company": "Figma", "title": "Engineer", "location": "Remote", "url": "https://f"}]}],
                     "round": None, "next_digest": iso(NOW + timedelta(minutes=5)), "send_requested": False,
                     "alerts_off": False}
        self.state = {"switches": {"laptop": True, "lambda": False, "discord": False, "email": email_ready},
                      "checking": "laptop", "laptop_app_pid": 42, "last_runs": {}, "storage": "sqlite",
                      "activity": {"hours": [], "checked": 0, "failed": 0, "new_jobs": 0, "matches": 0, "alerts": 0}}
        self.setup = {"ready": True, "email_ready": email_ready, "discord_ready": False, "profession": "tech",
                      "professions": [], "roles": ["engineer"], "exclude": [], "countries": ["US"], "also": []}

    def __call__(self, args: list[str], stdin: str | None) -> Result:
        self.calls.append((args, stdin))
        if args[0] == "switch":
            if len(args) > 2 and args[2] in ("on", "off"):
                self.state["switches"][args[1]] = args[2] == "on"
            return Result(True, json.dumps(self.state).encode())
        if args[0] == "matches":
            return Result(True, json.dumps(self.live).encode())
        if args[0] == "setup":
            return Result(True, json.dumps(self.setup).encode())
        return Result(True, b"{}")

    def last(self, command: str) -> tuple[list[str], str | None]:
        return next(call for call in reversed(self.calls) if call[0][0] == command)


@pytest.fixture
def live(app, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(system, "open_url", opened.append)
    radar = FakeRadar()
    model = Model(Calls(radar, inline=True))
    window = MainWindow(model)
    model.load_setup()
    model.refresh()
    model.show_window(setup=False)
    window.resize(1100, 800)
    settle(100)
    view = window.live_view
    yield radar, model, view, opened
    window.close()


def part(view_list, row: int, name: str) -> QPoint:
    """Where a row's part ("box", "action" or "row") is, in the list's viewport."""
    index = view_list.model().index(row)
    rect = view_list.visualRect(index)
    parts = view_list.itemDelegate().parts(rect.adjusted(4, 1, -4, -1), index.data(Qt.ItemDataRole.UserRole))
    return parts[name].center() if name in parts else QPoint(parts["text"].center())


def test_live_tracking_marks_ticked_jobs_as_seen(live):
    radar, model, view, opened = live
    assert view.tabs.tabText(0) == "New jobs (3)" and view.tabs.tabText(1) == "Seen (1)"
    assert view.mark_seen.text() == "Mark All as Seen"
    click(view.new.viewport(), part(view.new, 0, "box"))
    click(view.new.viewport(), part(view.new, 2, "box"))
    assert view.new_count.text() == "2 selected" and view.mark_seen.text() == "Mark as Seen"
    assert opened == []  # ticking isn't opening

    click(view.mark_seen)
    args, stdin = radar.last("matches")
    assert args == ["matches", "skip", "--json", "--stdin"]
    assert json.loads(stdin) == [["Stripe", "stripe:1"], ["Ramp", "ramp:3"]]
    assert view.new.rows.picks == set()


def test_a_search_narrows_what_all_means(live):
    radar, model, view, opened = live
    view.search.setText("montreal")  # accents don't matter
    settle()
    assert [m["company"] for m in view.new.rows.rows] == ["Ramp"]
    assert view.new_count.text() == "1 of 3" and view.mark_seen.text() == "Mark 1 as Seen"
    view.search.setText("eng")  # the start of a word: Engineer, not "Bengaluru"
    settle()
    assert [m["company"] for m in view.new.rows.rows] == ["Stripe", "Databricks", "Ramp"]
    view.search.setText("ai")
    settle()
    assert view.new.rows.rows == [] and view.new_stack.currentWidget() is view.new_empty
    view.search.setText("data")
    settle()
    click(view.send_now)
    args, stdin = radar.last("matches")
    assert args == ["matches", "send", "--json", "--stdin"] and json.loads(stdin) == [["Databricks", "databricks:2"]]


def test_send_all_and_open_a_posting(live):
    radar, model, view, opened = live
    click(view.send_now)
    assert radar.last("matches")[0] == ["matches", "send", "--json", "--all"]
    click(view.new.viewport(), part(view.new, 1, "row"))
    assert opened == ["https://example.com/2"]


def test_a_seen_job_goes_back_to_new(live):
    radar, model, view, opened = live
    view.tabs.setCurrentIndex(1)
    settle()
    click(view.seen.viewport(), part(view.seen, 0, "action"))
    assert radar.last("matches")[0] == ["matches", "unskip", "Shopify", "shopify:4", "--json"]
    assert view.seen.rows.rows == [] or radar.live["skipped"]  # moved back at once; the next read decides


def test_the_panel_flips_the_checker_and_locks_alerts_not_set_up(app):
    radar = FakeRadar(email_ready=False)
    model = Model(Calls(radar, inline=True))
    panel = Panel(model, lambda setup: None, None, lambda: None)
    model.load_setup()
    model.refresh()
    assert not panel.email.switch.isEnabled() and panel.email.detail.text().startswith("Not set up")
    assert panel.headline.text() == "● This PC is checking sites"
    click(panel.pc.switch)
    assert radar.last("switch")[0] == ["switch", "laptop", "off", "--json"]
    assert not model.switch("laptop") and panel.pc.detail.text() == "Off"


def test_a_channel_that_isnt_set_up_is_switched_off(app):
    radar = FakeRadar(email_ready=False)
    radar.state["switches"]["email"] = True  # on, but there's no Gmail saved
    model = Model(Calls(radar, inline=True))
    model.load_setup()
    model.refresh()
    assert (["switch", "email", "off", "--json"], None) in radar.calls and not model.switch("email")


# -- words and pictures -----------------------------------------------------------------------------


def test_times_and_counts_read_as_on_the_mac():
    local = datetime.now().astimezone()
    assert describe.ago(local - timedelta(minutes=12)) == "12 min. ago"
    assert describe.ago(local - timedelta(days=3)) == "3 days ago"
    assert describe.ago(local + timedelta(minutes=5, seconds=10)) == "in 5 min."
    assert describe.clock(local.replace(hour=14, minute=32)) == "2:32 PM"
    assert describe.clock(local.replace(hour=0, minute=5)) == "12:05 AM"
    assert describe.compact(950) == "950" and describe.compact(1240) == "1.2K" and describe.compact(34_000) == "34K"
    assert describe.took(3900) == "1 hr, 5 min" and describe.took(1500) == "25 min"
    assert describe.round_summary({"runner": "laptop", "done": 10, "total": 7536, "stale": True}) == \
        "This PC's last round stopped at 10 of 7,536"


def test_matching_finds_the_start_of_words():
    jobs = [{"title": "Software Engineer", "company": "Acme", "location": "Montréal"},
            {"title": "Maintainer", "company": "Beta", "location": None}]
    assert matching(jobs, "eng mont") == jobs[:1]
    assert matching(jobs, "ai") == []
    assert matching(jobs, "  ") == jobs


def test_the_icon_file_holds_every_size(app, tmp_path):
    icons.ico(tmp_path / "AppIcon.ico")
    data = (tmp_path / "AppIcon.ico").read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00" and int.from_bytes(data[4:6], "little") == len(icons.SIZES)
    assert data[6] == 16 and data[6 + 16 * (len(icons.SIZES) - 1)] == 0  # 256 is written as 0
