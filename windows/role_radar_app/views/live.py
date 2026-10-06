"""Live Tracking: the Mac app's LiveWindow. A sidebar with what's happening now (the round, the alerts,
the last 24 hours' activity), and the jobs: New jobs (the stack, newest on top), Seen, and the alerts
sent, as tabs. Ticked jobs, or all of them, can be marked as seen (never sent; undoable until the next
digest records it, within minutes) or sent now, alerts on or off. A search narrows the new jobs shown,
and what "all of them" means, to those whose title, company or place has its words. While it's open,
it's read every 5 seconds.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QLineEdit, QProgressBar, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QWidget,
)

from role_radar_app import describe, place, system
from role_radar_app.model import Match, Model, match_id
from role_radar_app.views import style
from role_radar_app.views.chart import ActivityView
from role_radar_app.views.jobs import JobList, box_area, matching, paint_box
from role_radar_app.views.style import SEMIBOLD, px, sized
from role_radar_app.views.widgets import Card, StatusLine, Trouble, button, column, label, row

READ_EVERY = 5_000  # ms, while the window shows Live Tracking


class SelectAll(QWidget):
    """The box above a list: ticks every job shown, or none; partly ticked when some are. Drawn as the
    rows' boxes are, in their column (jobs.box_area), so it sits in line with them."""

    clicked = Signal(bool)  # tick all of them, or none

    def __init__(self) -> None:
        super().__init__()
        self.state, self.shown, self.hover = Qt.CheckState.Unchecked, 0, False
        self.setToolTip("Select all")
        self.setAccessibleName("Select all")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(px(4) + px(4) + px(30), px(30))

    def show_count(self, chosen: int, shown: int) -> None:
        self.state = (Qt.CheckState.Unchecked if not chosen else
                      Qt.CheckState.Checked if chosen == shown else Qt.CheckState.PartiallyChecked)
        self.shown = shown
        self.setToolTip("Deselect all" if chosen else "Select all")
        self.update()

    def paintEvent(self, event) -> None:
        if self.shown:  # with nothing to tick, an empty space, so the heading stays put
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            paint_box(painter, self, box_area(px(4), 0, self.height()), self.state, self.hover)

    def enterEvent(self, event) -> None:
        self.hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self.shown and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.state == Qt.CheckState.Unchecked)


class LiveView(QWidget):
    def __init__(self, model: Model) -> None:
        super().__init__()
        self.model = model
        self._reader = QTimer(self, interval=READ_EVERY, timeout=model.refresh_live)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._sidebar())
        splitter.addWidget(self._lists())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([px(330), px(640)])
        layout = column(0)
        layout.addWidget(splitter)
        self.setLayout(layout)
        self.setMinimumSize(px(820), px(520))

        model.state_changed.connect(self._show_state)
        model.live_changed.connect(self._show_live)
        model.setup_changed.connect(self._show_live)
        self._show_state()
        self._show_live()

    # -- reading while shown ---------------------------------------------------------------------

    def showEvent(self, event) -> None:
        self.model.refresh_live()
        self._reader.start()

    def hideEvent(self, event) -> None:
        self._reader.stop()

    # -- the sidebar: what's happening now -------------------------------------------------------

    def _sidebar(self) -> QWidget:
        now = Card()
        now.body.addWidget(label("Now", 12, SEMIBOLD))
        if not place.CHECKS:
            now.body.addWidget(label("This dev build doesn't check job sites.", 11, tone="secondary"))
        self.round = StatusLine()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(px(6))
        self.failing = StatusLine()
        for widget in (self.round, self.progress, self.failing):
            now.body.addWidget(widget)

        alerts = Card()
        alerts.body.addWidget(label("Alerts", 12, SEMIBOLD))
        self.waiting = StatusLine()
        self.alerts_help = label("", 11, tone="secondary")
        alerts.body.addWidget(self.waiting)
        alerts.body.addWidget(self.alerts_help)

        self.activity_card = Card()
        self.activity = ActivityView()
        self.activity_card.body.addWidget(self.activity)
        self.trouble = Trouble(lambda: (self.model.retry(), self.model.refresh_live()))

        side = QWidget()
        layout = column(14, margins=16)
        for widget in (now, alerts, self.activity_card, self.trouble):
            layout.addWidget(widget)
        layout.addStretch(1)
        side.setLayout(layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(side)
        scroll.setMinimumWidth(px(300))
        scroll.setMaximumWidth(px(400))
        return scroll

    def _show_state(self) -> None:
        state, live = self.model.state, self.model.live
        info = (live or {}).get("round") or (state or {}).get("round")
        self.round.set(describe.round_summary(info), "↻")
        fraction = describe.round_fraction(info)
        self.progress.setVisible(fraction is not None)
        self.progress.setValue(round((fraction or 0) * 1000))
        failing = describe.failing_line(state)
        self.failing.setVisible(failing is not None)
        if failing:
            self.failing.set(failing[0], "✓" if failing[1] else "⚠", "green" if failing[1] else "orange")
        self.waiting.set(describe.waiting_line(state, live), "➤" if (live or {}).get("send_requested") else "▤")
        self.alerts_help.setText(self._alerts_help())
        activity = (state or {}).get("activity")
        self.activity_card.setVisible(bool(activity))
        if activity:
            self.activity.show_activity(activity)
        self.trouble.show_message(self.model.live_error or self.model.error)

    def _alerts_help(self) -> str:
        live = self.model.live
        if not live:
            return ""
        if live.get("send_requested"):
            return "Sending: this PC sends them within a minute."
        if not self.model.can_alert:
            return "No alerts set up: new jobs collect here. To send them, set up email or Discord in Edit Setup."
        if live.get("alerts_off"):
            return "Alerts are off: new jobs collect here until you send them or mark them as seen."
        return "New jobs go out every 10 minutes. Send some sooner, or mark the ones you don't want as seen."

    # -- the lists --------------------------------------------------------------------------------

    def _lists(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        sized(self.tabs, 12)
        self.tabs.addTab(self._new_jobs(), "New jobs")
        self.tabs.addTab(self._seen_jobs(), "Seen")
        self.tabs.addTab(self._sent_alerts(), "Sent alerts")
        self.loading = label("Loading matches…", 12, tone="secondary")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.loading)
        self.stack.addWidget(self.tabs)
        self.stack.setMinimumWidth(px(460))
        return self.stack

    def _new_jobs(self) -> QWidget:
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search new jobs by title, company or place")
        self.search.setClearButtonEnabled(True)
        sized(self.search, 13)
        # Ticks are for the jobs on screen: a new search starts with none, so a hidden job is never acted on.
        self.search.textChanged.connect(lambda: (self.new.rows.set_picks(set()), self._show_new()))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search, self.search.clear, context=Qt.ShortcutContext.WidgetShortcut)

        self.new_all = SelectAll()
        self.new_all.clicked.connect(self._pick_all_new)
        self.new_count = label("", 11, tone="secondary", wrap=False)
        self.mark_seen = button("Mark All as Seen", self._mark_seen, size=11,
                                tip="Take them off the stack. Jobs marked as seen are never sent.")
        self.send_now = button("Send All as Alert", self._send, size=11)
        bar = row(self.new_all, label("New jobs", 12, SEMIBOLD, wrap=False), self.new_count, self.mark_seen,
                  self.send_now, stretch_at=3)

        self.alerts_off_note = label("", 12, tone="secondary")
        self.new = JobList(seen=False, menu=self._new_menu)
        self.new.rows.picks_changed.connect(self._show_new_bar)
        self.new_empty = label("", 12, tone="secondary")
        self.new_empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.new_stack = QStackedWidget()
        self.new_stack.addWidget(self.new)
        self.new_stack.addWidget(self.new_empty)

        page = QWidget()
        layout = column(8, margins=12)
        layout.addWidget(self.search)
        layout.addLayout(bar)
        layout.addWidget(self.alerts_off_note)
        layout.addWidget(self.new_stack, 1)
        page.setLayout(layout)
        return page

    def _seen_jobs(self) -> QWidget:
        self.seen_all = SelectAll()
        self.seen_all.clicked.connect(self._pick_all_seen)
        self.seen_count = label("", 11, tone="secondary", wrap=False)
        self.mark_new = button("Mark as New", self._mark_new, size=11, tip="Put them back in New jobs")
        self.seen = JobList(seen=True)
        self.seen.acted.connect(lambda match: self.model.skip(match, False))
        self.seen.menu_for = lambda match: [("Mark as New", lambda: self.model.skip(match, False))]
        self.seen.rows.picks_changed.connect(self._show_seen_bar)
        self.seen_empty = label("Jobs you mark as seen go here for a week, and are never sent. Mark one as new to put it back.",
                                12, tone="secondary")
        self.seen_empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.seen_stack = QStackedWidget()
        self.seen_stack.addWidget(self.seen)
        self.seen_stack.addWidget(self.seen_empty)
        page = QWidget()
        layout = column(8, margins=12)
        layout.addLayout(row(self.seen_all, label("Seen", 12, SEMIBOLD, wrap=False), self.seen_count, self.mark_new,
                             stretch_at=3))
        layout.addWidget(self.seen_stack, 1)
        page.setLayout(layout)
        return page

    def _sent_alerts(self) -> QWidget:
        self.sent = QTreeWidget()
        self.sent.setColumnCount(2)
        self.sent.setHeaderHidden(True)
        self.sent.setStyleSheet("QTreeWidget { background: transparent; }")
        self.sent.setUniformRowHeights(False)
        self.sent.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.sent.setExpandsOnDoubleClick(False)
        self.sent.setMouseTracking(True)
        sized(self.sent, 12)
        self.sent.itemClicked.connect(self._sent_clicked)
        self.sent_empty = label("No alerts sent lately.", 12, tone="secondary")
        self.sent_empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.sent_stack = QStackedWidget()
        self.sent_stack.addWidget(self.sent)
        self.sent_stack.addWidget(self.sent_empty)
        page = QWidget()
        layout = column(8, margins=12)
        layout.addWidget(self.sent_stack, 1)
        page.setLayout(layout)
        self._sent_shown: list[Any] = []
        return page

    def _show_live(self) -> None:
        live = self.model.live
        if live is None:
            self.loading.setText(f"Couldn't load the matches\n\n{self.model.live_error}" if self.model.live_error
                                 else "Loading matches…")
            self.stack.setCurrentWidget(self.loading)
            self._show_state()
            return
        self.stack.setCurrentWidget(self.tabs)
        self.alerts_off_note.setVisible(bool(live.get("alerts_off")))
        self.alerts_off_note.setText(
            "Alerts are off: new jobs collect here, newest on top. Mark the ones you've seen, or send some as alerts."
            if self.model.can_alert else "New jobs collect here, newest on top. Mark the ones you've seen.")
        self._show_new()
        self.seen.show_rows(self.model.skipped)
        self.seen_stack.setCurrentWidget(self.seen if self.model.skipped else self.seen_empty)
        self._show_seen_bar()
        self._show_sent(live.get("sent") or [])
        self.tabs.setTabText(0, f"New jobs ({describe.number(len(self.model.waiting))})")
        self.tabs.setTabText(1, f"Seen ({describe.number(len(self.model.skipped))})")
        self.tabs.setTabText(2, f"Sent alerts ({describe.number(len(live.get('sent') or []))})")
        self._show_state()

    # -- new jobs ----------------------------------------------------------------------------------

    @property
    def searching(self) -> bool:
        return bool(self.search.text().strip())

    def shown(self) -> list[Match]:
        """The new jobs on screen: all of them, or those the search finds."""
        return matching(self.model.waiting, self.search.text())

    def _show_new(self) -> None:
        shown = self.shown()
        self.new.show_rows(shown)
        if not self.model.waiting:
            self.new_empty.setText("No new jobs. They appear here as soon as their company is checked.")
        elif not shown:
            self.new_empty.setText(f"No new jobs match “{self.search.text().strip()}”.")
        self.new_stack.setCurrentWidget(self.new if shown else self.new_empty)
        self._show_new_bar()

    def _targets(self) -> list[Match] | None:
        """What the bar's buttons act on: the ticked jobs; with none, all those the search shows (None: all)."""
        chosen = self.new.rows.picked()
        if chosen:
            return chosen
        return self.new.rows.pickable() if self.searching else None

    def _show_new_bar(self) -> None:
        open_jobs = self.new.rows.pickable()
        chosen = self.new.rows.picked()
        some = describe.number(len(open_jobs)) if self.searching else "All"
        self.new_all.show_count(len(chosen), len(open_jobs))
        total = describe.number(len(self.model.waiting))
        self.new_count.setText(f"{len(chosen)} selected" if chosen
                               else f"{describe.number(len(self.shown()))} of {total}" if self.searching else total)
        busy = bool({"seen", "send"} & self.model.live_busy)
        self.mark_seen.setText("Mark as Seen" if chosen else f"Mark {some} as Seen")
        self.mark_seen.setEnabled(bool(open_jobs) and not busy)
        self.send_now.setText("Send as Alert" if chosen else f"Send {some} as Alert")
        self.send_now.setEnabled(bool(open_jobs) and not busy and self.model.can_alert)
        self.send_now.setToolTip("Send them by email or Discord now; once sent, they leave the stack."
                                 if self.model.can_alert else "Set up email or Discord in Edit Setup to send jobs.")

    def _pick_all_new(self, on: bool) -> None:
        self.new.rows.set_picks({match_id(m) for m in self.new.rows.pickable()} if on else set())

    def _mark_seen(self) -> None:
        targets = self._targets()
        self.new.rows.set_picks(set())
        self.model.mark_seen(targets)

    def _send(self) -> None:
        targets = self._targets()
        self.new.rows.set_picks(set())
        self.model.send(targets)

    def _new_menu(self, match: Match) -> list[tuple[str, Any]]:
        items = [("Mark as Seen", lambda: self.model.mark_seen([match]))]
        if self.model.can_alert:
            items.append(("Send as Alert", lambda: self.model.send([match])))
        return items

    # -- seen jobs ---------------------------------------------------------------------------------

    def _show_seen_bar(self) -> None:
        chosen = self.seen.rows.picked()
        self.seen_all.show_count(len(chosen), len(self.seen.rows.rows))
        self.seen_count.setText(f"{len(chosen)} selected" if chosen else describe.number(len(self.model.skipped)))
        self.mark_new.setVisible(bool(chosen))
        self.mark_new.setEnabled("new" not in self.model.live_busy)

    def _pick_all_seen(self, on: bool) -> None:
        self.seen.rows.set_picks({match_id(m) for m in self.seen.rows.rows} if on else set())

    def _mark_new(self) -> None:
        chosen = self.seen.rows.picked()
        self.seen.rows.set_picks(set())
        self.model.mark_new(chosen)

    # -- sent alerts -------------------------------------------------------------------------------

    def _show_sent(self, sent: list[dict[str, Any]]) -> None:
        self.sent_stack.setCurrentWidget(self.sent if sent else self.sent_empty)
        if sent == self._sent_shown:
            self._label_sent()  # only "12 min. ago" changes
            return
        opened = {self.sent.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                  for i in range(self.sent.topLevelItemCount()) if self.sent.topLevelItem(i).isExpanded()}
        self.sent.clear()
        for alert in sent:
            item = QTreeWidgetItem(self.sent)
            item.setData(0, Qt.ItemDataRole.UserRole, alert.get("sent_at"))
            item.setToolTip(0, "Show this alert's jobs")
            for job in alert.get("jobs") or []:
                child = QTreeWidgetItem(item)
                child.setFirstColumnSpanned(True)
                place_line = " · ".join(part for part in (job.get("company"), job.get("location")) if part)
                child.setText(0, f"{job.get('title') or ''}\n{place_line}")
                child.setData(0, Qt.ItemDataRole.UserRole, job.get("url"))
                child.setToolTip(0, "Open the posting in your browser" if job.get("url") else "")
            item.setExpanded(alert.get("sent_at") in opened)
        self._sent_shown = sent
        self._label_sent()

    def _label_sent(self) -> None:
        for index, alert in enumerate(self._sent_shown):
            item = self.sent.topLevelItem(index)
            sent = describe.date(alert.get("sent_at"))
            by = "Lambda" if alert.get("by") == "lambda" else (None if alert.get("by") is None else "this PC")
            parts = [describe.plural(len(alert.get("jobs") or []), "job"), f"sent by {by}" if by else None,
                     describe.ago(sent) if sent else None]
            item.setText(0, describe.full(sent) if sent else alert.get("sent_at"))
            item.setFont(0, style.font(13, SEMIBOLD))
            item.setText(1, " · ".join(p for p in parts if p))
            item.setForeground(1, style.secondary())
        self.sent.resizeColumnToContents(0)

    def _sent_clicked(self, item: QTreeWidgetItem) -> None:
        if item.parent() is None:
            item.setExpanded(not item.isExpanded())
        elif url := item.data(0, Qt.ItemDataRole.UserRole):
            system.open_url(url)
