"""The panel that opens from the tray icon: the Mac app's menu bar Panel. Who's checking, the checker's
switch and the alerts', the round and the jobs waiting, the way into Live Tracking or Setup, Open at
Login, the log, Quit, and updates. It closes when clicked away from, as Windows' own flyouts do.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QCheckBox, QFrame, QToolButton, QWidget

from role_radar_app import describe, place, system
from role_radar_app.model import Model
from role_radar_app.views import style
from role_radar_app.views.style import SEMIBOLD, px, sized
from role_radar_app.views.widgets import Card, StatusLine, SwitchRow, Trouble, button, column, label, link, row


class Panel(QWidget):
    def __init__(self, model: Model, open_window: Callable[[bool], None], check_updates: Callable[[], None] | None,
                 quit_app: Callable[[], None]) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.model = model
        self.open_window = open_window

        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setAutoRaise(True)
        refresh.setToolTip("Refresh")
        sized(refresh, 13)
        refresh.clicked.connect(lambda: model.refresh())
        self.headline = label("", 12, wrap=False)

        self.pc = SwitchRow(describe.WHO, "pc")
        self.pc.flipped.connect(lambda on: model.set("laptop", on))
        runners = Card(10)
        runners.body.addWidget(self.pc)

        self.discord = SwitchRow("Discord", "chat")
        self.email = SwitchRow("Email", "mail")
        self.discord.flipped.connect(lambda on: model.set("discord", on))
        self.email.flipped.connect(lambda on: model.set("email", on))
        alerts = Card(10)
        alerts.body.addWidget(self.discord)
        alerts.body.addWidget(self.email)
        self.alerts_note = label("", 11, tone="secondary")

        # Until Setup is done, the way to it; then the round, the jobs waiting, and Live Tracking.
        self.unready = Card()
        self.unready.body.addWidget(label("Finish setting up: pick your profession, countries and the roles you want.", 11,
                                          tone="secondary"))
        self.unready.body.addWidget(button("Set Up Role Radar", lambda: self._open(True), size=13, primary=True))
        self.health = Card()
        self.round, self.waiting, self.failing = StatusLine(), StatusLine(), StatusLine()
        for line in (self.round, self.waiting, self.failing):
            self.health.body.addWidget(line)
        self.health.body.addWidget(button("Live Tracking", lambda: self._open(False), size=13, primary=True,
                                          tip="Matches waiting to be sent, seen and sent, the round in progress, and activity"))
        self.trouble = Trouble(model.retry)

        self.login = QCheckBox("Open at Login")
        sized(self.login, 12)
        self.login.setVisible(place.WINDOWS and place.INSTALLED is not None and not place.DEV)
        self.login.clicked.connect(self._set_login)
        footer = row(self.login,
                     button("Edit Setup…", lambda: self._open(True), size=12,
                            tip="Your profession, countries, roles, qualifications and alerts"),
                     button("Log", lambda: system.open_file(place.checker_log()), size=12),
                     button("Quit", quit_app, size=12, tip="Also stops this PC's checker"), stretch_at=1)
        self.version = row(label(f"Version {place.VERSION}", 11, tone="secondary", wrap=False),
                           link("Check for Updates…", check_updates, size=11) if check_updates else None, stretch_at=1)

        frame = QFrame()
        frame.setObjectName("popover")
        layout = column(12, margins=14)
        layout.addLayout(row(label("Role Radar", 13, SEMIBOLD, wrap=False), refresh, stretch_at=1))
        layout.addWidget(self.headline)
        layout.addWidget(runners)
        layout.addWidget(label("This PC checks while this app is open. Everything stays on this PC.", 11, tone="secondary"))
        layout.addWidget(label("Alerts", 12, SEMIBOLD))
        layout.addWidget(alerts)
        layout.addWidget(self.alerts_note)
        layout.addWidget(self.unready)
        layout.addWidget(self.health)
        layout.addWidget(self.trouble)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {style.css(style.faded(style.text(), 0.12))};")
        layout.addWidget(line)
        layout.addLayout(footer)
        layout.addLayout(self.version)
        frame.setLayout(layout)
        outer = column(0)
        outer.addWidget(frame)
        self.setLayout(outer)

        model.state_changed.connect(self._show)
        model.setup_changed.connect(self._show)
        style.text_size.changed.connect(self._fit)
        self._show()

    # -- showing it beside the tray -----------------------------------------------------------------

    def _fit(self) -> None:
        """Wider as the text grows, so lines don't wrap into a column."""
        self.setFixedWidth(max(px(330), round(290 * style.text_size.scale * 1.15)))
        self.adjustSize()

    def show_at(self, icon: QRect | None) -> None:
        """Open beside the tray icon: above it with the taskbar at the bottom, below it with the taskbar
        (or a Mac's menu bar) at the top."""
        self.model.refresh()
        self._fit()
        screen = (QGuiApplication.screenAt(icon.center()) if icon and icon.isValid() else None) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if icon and icon.isValid():
            x = icon.center().x() - self.width() // 2
            y = icon.top() - self.height() - 8 if icon.center().y() > area.center().y() else icon.bottom() + 8
        else:
            x, y = area.right() - self.width() - 12, area.bottom() - self.height() - 12
        self.move(QPoint(max(area.left() + 8, min(x, area.right() - self.width() - 8)),
                         max(area.top() + 8, min(y, area.bottom() - self.height() - 8))))
        self.login.setChecked(system.open_at_login())
        self.show()
        self.activateWindow()

    def _open(self, setup: bool) -> None:
        self.hide()
        self.open_window(setup)

    def _set_login(self, on: bool) -> None:
        try:
            system.set_open_at_login(on)
        except OSError as error:
            self.model.error = f"Open at Login: {error}"
            self.model.state_changed.emit()
        self.login.setChecked(system.open_at_login())

    # -- what it shows ---------------------------------------------------------------------------------

    def _show(self) -> None:
        self._show_headline()
        self._show_checker()
        self._show_alerts()
        self._show_health()
        self.trouble.show_message(self.model.error or self.model.setup_error)
        if self.isVisible():
            self.adjustSize()

    def _show_headline(self) -> None:
        model = self.model
        if model.state is None:
            text, tone = ("Can't read the status", "orange") if model.error else ("Loading…", "secondary")
        elif model.checking == "laptop":
            text, tone = f"● {describe.WHO} is checking sites", "green"
        else:
            text, tone = "● Nothing is checking sites", "orange"
        self.headline.setText(text)
        style.tone(self.headline, tone)

    def _last_pass(self) -> str:
        runs = (self.model.state or {}).get("last_runs") or {}
        last = max((describe.date(run["finished_at"]) for name, run in runs.items()
                    if name.startswith("laptop") and run.get("finished_at")), default=None)
        return describe.ago(last)

    def _show_checker(self) -> None:
        model, state = self.model, self.model.state
        on = model.switch("laptop")
        if state is None:
            detail = ""
        elif not on:
            detail = "Off"
        elif model.checking == "laptop":
            detail = f"Checking · last pass {self._last_pass()}"
        else:
            detail = "On, but role-radar start isn't running"
        # Switched on but not running: offer to start it.
        needs_start = state is not None and on and not state.get("laptop_app_pid") and model.can_start
        self.pc.show_state(detail, on, model.checking == "laptop", "laptop" in model.busy, locked=state is None,
                           action=("Start", lambda: model.set("laptop", True)) if needs_start else None)

    def _show_alerts(self) -> None:
        model = self.model
        for name, line in (("discord", self.discord), ("email", self.email)):
            ready = model.channel_ready(name)
            switched = ready and model.switch(name)
            if not ready:
                detail = "Not set up · add it in Edit Setup…"
            else:
                detail = "New jobs are sent here" if switched else "Off"
            line.show_state(detail, switched, switched, name in model.busy,
                            locked=model.state is None or (not ready and not switched))
        alerts_off = model.state is not None and not model.switch("discord") and not model.switch("email")
        self.alerts_note.setText("Alerts are optional. Off, new jobs collect in Live Tracking, newest on top."
                                 if alerts_off else "New jobs go to the alerts switched on, every 10 minutes.")

    def _show_health(self) -> None:
        model, state = self.model, self.model.state
        self.unready.setVisible(model.setup is not None and not model.ready)
        self.health.setVisible(model.ready and state is not None)
        if state is None:
            return
        self.round.set(describe.round_summary(state.get("round")), "↻")
        self.waiting.set(describe.waiting_line(state, None), "▤")
        failing = describe.failing_line(state)
        self.failing.setVisible(failing is not None)
        if failing:
            self.failing.set(failing[0], "✓" if failing[1] else "⚠", "green" if failing[1] else "orange")
