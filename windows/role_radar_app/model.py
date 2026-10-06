"""The app's state and what it does, as the Mac app's Model: what `role-radar switch --json` reports
(the switches, who's checking, the round, activity), Live Tracking's lists (`matches --json`), and
Setup's answers (`setup show`). Views watch its signals and call its actions; every action is a
role-radar command (cli.py), and its reply is the new state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from role_radar_app import place, system
from role_radar_app.cli import Calls, Result

log = logging.getLogger(__name__)

REFRESH = 60_000  # ms between re-reading the state (and restarting the checker if it stopped)
CHECK_IN_FIRST, CHECK_IN_EVERY = 30_000, 6 * 3600_000  # the anonymous check-in, for the user counts
Match = dict[str, Any]


def match_id(match: Match) -> str:
    return f"{match['company']}#{match['uid']}"


def iso_now() -> str:
    from role_radar.storage import to_iso, utcnow

    return to_iso(utcnow())


class Model(QObject):
    state_changed = Signal()  # state, busy, error
    live_changed = Signal()  # live, live_busy, live_error
    setup_changed = Signal()  # setup, setup_error
    window_wanted = Signal()  # open the window: Setup's pages or Live Tracking, as showing_setup says

    def __init__(self, calls: Calls, launched_at_login: bool = False) -> None:
        super().__init__()
        self.calls = calls
        self.launched_at_login = launched_at_login
        self.state: dict[str, Any] | None = None
        self.busy: set[str] = set()  # switches being flipped
        self.error: str | None = None
        self.live: dict[str, Any] | None = None
        self.live_busy: set[str] = set()  # match ids, "seen", "new" or "send" in flight
        self.live_error: str | None = None
        self._live_actions = 0  # a refresh that started before the latest action is out of date
        self._live_data: bytes | None = None  # the lists as last read: the same again changes nothing
        self.setup: dict[str, Any] | None = None
        self.setup_error: str | None = None  # why Setup couldn't read or save its files: always shown
        self.showing_setup = False  # the window shows Setup's pages rather than Live Tracking
        self._timer = QTimer(self, interval=REFRESH, timeout=self.refresh)
        self._check_in = QTimer(self, singleShot=True, timeout=self._send_check_in)

    # -- starting up ---------------------------------------------------------------------------

    def start(self) -> None:
        """Create the app's files, open Setup until it's done (or whenever someone opens the app rather
        than it opening at login), then re-read the state every minute."""
        def prepared(_: Any = None) -> None:
            if not self.ready or not self.launched_at_login:
                self.show_window(setup=not self.ready)
            self.refresh()
            self._timer.start()

        def initialized(result: Result) -> None:
            if not result.ok:
                self._set_setup_error(result.message)
            self.load_setup(then=prepared)

        self.setup_command(["init"], then=initialized)
        if not place.DEV:
            self._check_in.start(CHECK_IN_FIRST)  # once Setup's files exist, and the network is up after a login

    def _send_check_in(self) -> None:
        self.calls.call(["checkin"])
        self._check_in.start(CHECK_IN_EVERY)

    def show_window(self, setup: bool | None = None) -> None:
        if setup is not None:
            self.showing_setup = setup
        self.window_wanted.emit()

    # -- what it knows ---------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Setup is done: a profession, countries and roles."""
        return bool(self.setup and self.setup.get("ready"))

    @property
    def can_start(self) -> bool:
        """It starts checking only once Setup is done (and, in a dev run, only if asked to check)."""
        return place.CHECKS and self.ready

    @property
    def checking(self) -> str | None:
        return (self.state or {}).get("checking")

    def switch(self, name: str) -> bool:
        return bool(((self.state or {}).get("switches") or {}).get(name))

    def channel_ready(self, name: str) -> bool:
        """Whether an alert channel is set up in Setup."""
        if not self.setup:
            return False
        return bool(self.setup.get("email_ready") if name == "email" else self.setup.get("discord_ready"))

    @property
    def can_alert(self) -> bool:
        """Whether alerts can be sent at all: email or Discord is set up."""
        return self.channel_ready("email") or self.channel_ready("discord")

    # -- the switches ---------------------------------------------------------------------------

    def refresh(self, then: Callable[[], None] | None = None) -> None:
        """Re-read the state, starting the checker first if it's switched on and not running."""
        def fix_channels() -> None:
            # A channel that isn't set up can't send: keep its switch off, so every view says so.
            off = [name for name in ("discord", "email") if self.switch(name) and not self.channel_ready(name)]
            if off:
                self._run(["switch", off[0], "off", "--json"], then=fix_channels)
            elif then:
                then()

        self._run(["switch", "--json"] + (["--start"] if self.can_start else []), then=fix_channels)

    def set(self, name: str, on: bool, then: Callable[[], None] | None = None) -> None:
        """Flip a switch: "laptop" (the checker), "discord" or "email" (alerts)."""
        self.busy.add(name)
        if self.state:
            self.state.setdefault("switches", {})[name] = on
        self.state_changed.emit()

        def done() -> None:
            self.busy.discard(name)
            self.state_changed.emit()
            if then:
                then()

        # --start: switching the checker on also starts it if it isn't running (not during Setup).
        self._run(["switch", name, "on" if on else "off", "--json"] + (["--start"] if self.can_start else []), then=done)

    def _run(self, args: list[str], then: Callable[[], None] | None = None) -> None:
        def finished(result: Result) -> None:
            if result.ok:
                try:
                    self.state, self.error = result.json(), None
                except ValueError as error:
                    self.error = f"Unexpected reply from role-radar: {error}"
            else:
                self.error = result.message
                self._refresh_quietly()
            self.state_changed.emit()
            if then:
                then()

        self.calls.call(args, then=finished)

    def _refresh_quietly(self) -> None:
        """After a failed switch, re-read the real state so the switches don't lie."""
        def finished(result: Result) -> None:
            if result.ok:
                try:
                    self.state = result.json()
                except ValueError:
                    return
                self.state_changed.emit()

        self.calls.call(["switch", "--json"], then=finished)

    def start_checking(self, then: Callable[[], None] | None = None) -> None:
        """Setup is done: start checking now and at every login (a dev run doesn't open at login)."""
        if not place.DEV:
            system.set_open_at_login(True)
        self.set("laptop", True, then=then)

    def stop_checker(self) -> None:
        """Ask the checker to finish the companies in flight and quit, without waiting: on quit."""
        try:
            from role_radar_app import cli

            cli.start(["stop"])
        except OSError as error:
            log.warning("Couldn't stop the checker: %s", error)

    def retry(self) -> None:
        """Try again after a failure: the state, and Setup's."""
        self.refresh()
        self.load_setup()

    # -- Live Tracking ----------------------------------------------------------------------------

    def refresh_live(self) -> None:
        seen = self._live_actions
        self._run_live(["matches", "--json"], unless=lambda: self._live_actions != seen)

    def skip(self, match: Match, on: bool) -> None:
        """Mark one match as seen, or put it back."""
        self._live_action(match_id(match))
        self._set_skipped({match_id(match)}, on)
        self._run_live(["matches", "skip" if on else "unskip", match["company"], match["uid"], "--json"],
                       done=lambda: self._live_done(match_id(match)))

    def mark_seen(self, matches: list[Match] | None) -> None:
        """Mark matches as seen (None: all of them): they leave the stack, and are never sent."""
        self._live_action("seen")
        self._set_skipped({match_id(m) for m in (self.waiting if matches is None else matches)}, True)
        self._run_live(["matches", "skip", "--json", *self._picks(matches)], stdin=self._pick_list(matches),
                       done=lambda: self._live_done("seen"))

    def mark_new(self, matches: list[Match]) -> None:
        """Put seen matches back among the new ones, while the next digest hasn't recorded them yet."""
        self._live_action("new")
        self._set_skipped({match_id(m) for m in matches}, False)
        self._run_live(["matches", "unskip", "--json", *self._picks(matches)], stdin=self._pick_list(matches),
                       done=lambda: self._live_done("new"))

    def send(self, matches: list[Match] | None) -> None:
        """Send matches now (None: all of them), alerts on or off; once sent, they leave the stack."""
        self._live_action("send")
        ids = {match_id(m) for m in (self.waiting if matches is None else matches)}
        stamp = iso_now()
        for match in self.waiting:
            if match_id(match) in ids:
                match["send_at"] = stamp
        self.live_changed.emit()
        self._run_live(["matches", "send", "--json", *self._picks(matches)], stdin=self._pick_list(matches),
                       done=lambda: self._live_done("send"))

    @property
    def waiting(self) -> list[Match]:
        return (self.live or {}).get("waiting") or []

    @property
    def skipped(self) -> list[Match]:
        return (self.live or {}).get("skipped") or []

    def _live_action(self, key: str) -> None:
        self._live_actions += 1
        self._live_data = None  # what's shown changed already: take whatever comes back
        self.live_busy.add(key)

    def _live_done(self, key: str) -> None:
        self.live_busy.discard(key)
        self.live_changed.emit()

    def _set_skipped(self, ids: set[str], on: bool) -> None:
        """Move matches between New and Seen straight away, before the command confirms it."""
        if not self.live:
            return
        source = "waiting" if on else "skipped"
        moved = [m for m in self.live[source] if match_id(m) in ids]
        self.live[source] = [m for m in self.live[source] if match_id(m) not in ids]
        for match in moved:
            match["skipped_at"] = iso_now() if on else None
        if on:
            self.live["skipped"] = moved + self.live["skipped"]
        else:
            self.live["waiting"] = sorted(self.live["waiting"] + moved, key=lambda m: m.get("found_at") or m["first_seen"],
                                          reverse=True)
        self.live_changed.emit()

    @staticmethod
    def _picks(matches: list[Match] | None) -> list[str]:
        """All of them (--all), or the picked ones as JSON on stdin: a process can only take so many arguments."""
        return ["--all"] if matches is None else ["--stdin"]

    @staticmethod
    def _pick_list(matches: list[Match] | None) -> str | None:
        return None if matches is None else json.dumps([[m["company"], m["uid"]] for m in matches])

    def _run_live(self, args: list[str], stdin: str | None = None, unless: Callable[[], bool] = lambda: False,
                  done: Callable[[], None] | None = None) -> None:
        def finished(result: Result) -> None:
            try:
                self._take_live(result, unless)
            finally:
                if done:
                    done()

        self.calls.call(args, then=finished, stdin=stdin)

    def _take_live(self, result: Result, outdated: Callable[[], bool]) -> None:
        if outdated():
            return
        if result.ok:
            if result.data == self._live_data:
                if self.live_error:
                    self.live_error = None
                    self.live_changed.emit()
                return
            try:
                self.live, self._live_data, self.live_error = result.json(), result.data, None
            except ValueError as error:
                self.live_error = f"Unexpected reply from role-radar: {error}"
        else:
            self.live_error = result.message
        self.live_changed.emit()

    # -- Setup ---------------------------------------------------------------------------------

    def load_setup(self, then: Callable[[], None] | None = None) -> None:
        def finished(result: Result) -> None:
            if result.ok:
                try:
                    self.setup, self.setup_error = result.json(), None
                except ValueError as error:
                    self.setup_error = f"Unexpected reply from role-radar: {error}"
            else:
                self.setup_error = result.message
            self.setup_changed.emit()
            if then:
                then()

        self.setup_command(["show"], then=finished)

    def setup_step(self, args: list[str], data: dict[str, Any], then: Callable[[str | None], None] | None = None) -> None:
        """Run `role-radar setup ARGS` with `data` on stdin, taking the setup it reports. `then` gets
        the error message, or None."""
        def finished(result: Result) -> None:
            if result.ok:
                try:
                    self.setup = result.json()
                    self.setup_changed.emit()
                except ValueError:
                    pass
            if then:
                then(None if result.ok else result.message)

        self.setup_command(args, then=finished, stdin=json.dumps(data, sort_keys=True))

    def setup_command(self, args: list[str], then: Callable[[Result], None] | None = None, stdin: str | None = None) -> None:
        self.calls.call(["setup", *args], then=then, stdin=stdin)

    def _set_setup_error(self, message: str | None) -> None:
        self.setup_error = message
        self.setup_changed.emit()
