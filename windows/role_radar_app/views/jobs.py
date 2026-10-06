"""Live Tracking's job lists: the new jobs and those marked as seen, each row the Mac app's MatchRow.

A row shows the title, its company and place, and when it was found (or seen). Clicking it opens the
posting; its tick box picks it for acting on several at once; a seen job has its own Mark as New.
Right-click for the rest. The rows are drawn rather than made of widgets, so a list of a few thousand
new jobs scrolls and ticks as fast as a short one.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Callable

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QListView, QMenu, QStyle, QStyledItemDelegate, QStyleOptionButton, QStyleOptionViewItem, QWidget,
)

from role_radar_app import describe, system
from role_radar_app.model import Match, match_id
from role_radar_app.views import style
from role_radar_app.views.style import MEDIUM, px

ROW = Qt.ItemDataRole.UserRole


def matching(jobs: list[Match], query: str) -> list[Match]:
    """The jobs with every word of `query` starting a word of their title, company or place, ignoring
    case and accents: "eng" finds Engineer and "montreal" Montréal, but "ai" doesn't find Maintain."""
    words = [_fold(word) for word in query.split()]
    if not words:
        return jobs
    found = []
    for job in jobs:
        text = _fold(" ".join([job.get("title") or "", job.get("company") or "", job.get("location") or ""]))
        if all(_starts_a_word(word, text) for word in words):
            found.append(job)
    return found


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _starts_a_word(word: str, text: str) -> bool:
    start = text.find(word)
    while start != -1:
        if start == 0 or not text[start - 1].isalnum():
            return True
        start = text.find(word, start + 1)
    return False


class JobRows(QAbstractListModel):
    """One list's jobs, and which of them are ticked."""

    picks_changed = Signal()

    def __init__(self, seen: bool) -> None:
        super().__init__()
        self.seen = seen  # the Seen list: dimmed, "Seen …", Mark as New
        self.rows: list[Match] = []
        self.picks: set[str] = set()

    def set_rows(self, rows: list[Match]) -> None:
        if rows == self.rows:
            return
        self.beginResetModel()
        self.rows = [dict(m) for m in rows]  # copies: the model changes its own in place (Sending…)
        self.endResetModel()
        ids = {match_id(m) for m in rows if not m.get("send_at")}
        if not self.picks <= ids:
            self.picks &= ids  # a ticked job that's gone (or on its way out) isn't ticked any more
            self.picks_changed.emit()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        match = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return match.get("title")
        if role == Qt.ItemDataRole.ToolTipRole:
            return "Open the posting in your browser" if match.get("url") else None
        if role == ROW:
            return match
        return None

    def pickable(self) -> list[Match]:
        """The jobs a tick box can pick: not those on their way out."""
        return [m for m in self.rows if not m.get("send_at")]

    def picked(self) -> list[Match]:
        return [m for m in self.rows if match_id(m) in self.picks]

    def toggle(self, row: int) -> None:
        match = self.rows[row]
        if match.get("send_at"):
            return
        self.picks ^= {match_id(match)}
        self.dataChanged.emit(self.index(row), self.index(row))
        self.picks_changed.emit()

    def set_picks(self, ids: set[str]) -> None:
        if ids != self.picks:
            self.picks = set(ids)
            if self.rows:
                self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))
            self.picks_changed.emit()


def box_area(row_left: int, top: int, height: int) -> QRect:
    """Where a row's tick box goes: a column at its left, which the box above the list shares."""
    return QRect(row_left + px(4), top, px(30), height)


def paint_box(painter: QPainter, widget: QWidget, area: QRect, state: Qt.CheckState, hover: bool) -> None:
    """A tick box as the system draws one, centered in `area`, with a round highlight under the pointer (as in Gmail)."""
    if hover:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(style.faded(style.text(), 0.09))
        side = px(28)
        painter.drawEllipse(QRectF(area.center().x() - side / 2 + 1, area.center().y() - side / 2 + 1, side, side))
    check = QStyleOptionButton()
    indicator = widget.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth)
    check.rect = QRect(area.center().x() - indicator // 2 + 1, area.center().y() - indicator // 2 + 1, indicator, indicator)
    check.state = QStyle.StateFlag.State_Enabled | {Qt.CheckState.Checked: QStyle.StateFlag.State_On,
                                                    Qt.CheckState.PartiallyChecked: QStyle.StateFlag.State_NoChange}.get(
        state, QStyle.StateFlag.State_Off)
    widget.style().drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, check, painter, widget)


class JobDelegate(QStyledItemDelegate):
    """Draws a row: tick box, title (up to two lines), company · place, when, and its button or note."""

    def __init__(self, view: JobList) -> None:
        super().__init__(view)
        self.view = view

    # -- where each part goes ---------------------------------------------------------------------

    def parts(self, rect: QRect, match: Match) -> dict[str, QRect]:
        pad = px(6)
        box = box_area(rect.left(), rect.top(), rect.height())
        right = rect.right() - px(8)
        action = None
        if self.view.rows.seen:
            width = QFontMetrics(style.font(11)).horizontalAdvance("Mark as New") + px(20)
            action = QRect(right - width, rect.center().y() - px(12), width, px(24))
            right = action.left() - px(8)
        elif match.get("send_at"):
            width = QFontMetrics(style.font(11)).horizontalAdvance("Sending…")
            action = QRect(right - width, rect.top(), width, rect.height())
            right = action.left() - px(8)
        else:
            right -= px(18)  # the ↗ shown under the pointer
        text = QRect(box.right() + px(6), rect.top() + pad, max(10, right - box.right() - px(6)), rect.height() - 2 * pad)
        return {"box": box, "text": text, **({"action": action} if action else {})}

    def _title_height(self, width: int, title: str) -> int:
        metrics = QFontMetrics(style.font(13, MEDIUM))
        lines = metrics.boundingRect(QRect(0, 0, width, 10_000), Qt.TextFlag.TextWordWrap, title).height()
        return min(lines, 2 * metrics.lineSpacing())

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        match = index.data(ROW)
        width = self.view.viewport().width()
        text = self.parts(QRect(0, 0, width, 40), match)["text"]
        small = QFontMetrics(style.font(11)).lineSpacing()
        return QSize(width, self._title_height(text.width(), match.get("title") or "") + 2 * small + px(6) + 2 * px(6))

    # -- drawing ---------------------------------------------------------------------------------

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        match: Match = index.data(ROW)
        rect = option.rect.adjusted(px(4), 1, -px(4), -1)
        parts = self.parts(rect, match)
        picked = match_id(match) in self.view.rows.picks
        hover_row = self.view.hover[0] == index.row()
        part = self.view.hover[1] if hover_row else None
        link = bool(match.get("url"))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if picked or (hover_row and part == "row" and link):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(style.faded(style.accent(), 0.14) if picked else style.faded(style.text(), 0.06))
            painter.drawRoundedRect(QRectF(rect), 7, 7)
        if self.view.rows.seen:
            painter.setOpacity(0.6)

        box = parts["box"]
        if match.get("send_at"):
            painter.setPen(style.secondary())
            painter.setFont(style.font(12))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "➤")
        else:
            paint_box(painter, self.view, box, Qt.CheckState.Checked if picked else Qt.CheckState.Unchecked, part == "box")

        text = parts["text"]
        title_font = style.font(13, MEDIUM)
        title_height = self._title_height(text.width(), match.get("title") or "")
        painter.setFont(title_font)
        painter.setPen(style.accent() if hover_row and part == "row" and link else style.text())
        title_box = QRect(text.left(), text.top(), text.width(), title_height)
        painter.drawText(title_box, Qt.TextFlag.TextWordWrap, self._elide_lines(match.get("title") or "", title_font, text.width()))
        small = style.font(11)
        metrics = QFontMetrics(small)
        painter.setFont(small)
        painter.setPen(style.secondary())
        where = " · ".join(piece for piece in (match.get("company"), match.get("location")) if piece)
        y = title_box.bottom() + px(3)
        painter.drawText(QRect(text.left(), y, text.width(), metrics.lineSpacing()), Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(where, Qt.TextElideMode.ElideRight, text.width()))
        painter.setPen(style.tertiary())
        when = (f"Seen {describe.stamp(match.get('skipped_at'))}" if self.view.rows.seen
                else f"Found {describe.stamp(match.get('found_at') or match.get('first_seen'))}")
        painter.drawText(QRect(text.left(), y + metrics.lineSpacing(), text.width(), metrics.lineSpacing()),
                         Qt.AlignmentFlag.AlignLeft, when)

        painter.setOpacity(1)
        action = parts.get("action")
        if self.view.rows.seen and action:
            self._draw_button(painter, action, "Mark as New", hover=part == "action")
        elif action:
            painter.setPen(style.secondary())
            painter.drawText(action, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "Sending…")
        elif hover_row and part == "row" and link:
            painter.setPen(style.accent())
            painter.setFont(style.font(12, style.SEMIBOLD))
            painter.drawText(QRect(rect.right() - px(22), rect.top(), px(16), rect.height()), Qt.AlignmentFlag.AlignCenter, "↗")
        painter.restore()

    @staticmethod
    def _elide_lines(title: str, font, width: int) -> str:
        """The title in at most two lines: the second cut short with … if it doesn't fit."""
        metrics = QFontMetrics(font)
        if metrics.boundingRect(QRect(0, 0, width, 10_000), Qt.TextFlag.TextWordWrap, title).height() <= 2 * metrics.lineSpacing():
            return title
        words, first = title.split(), ""
        while words and metrics.horizontalAdvance((first + " " + words[0]).strip()) <= width:
            first = (first + " " + words.pop(0)).strip()
        return first + "\n" + metrics.elidedText(" ".join(words), Qt.TextElideMode.ElideRight, width)

    def _draw_button(self, painter: QPainter, rect: QRect, text: str, hover: bool) -> None:
        painter.setPen(QPen(style.faded(style.text(), 0.25), 1))
        painter.setBrush(style.faded(style.text(), 0.1 if hover else 0.04))
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        painter.setPen(style.text())
        painter.setFont(style.font(11))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class JobList(QListView):
    """A list of jobs. Clicks: the tick box picks a job, the button acts on it, the rest opens it."""

    acted = Signal(dict)  # a seen job's Mark as New

    def __init__(self, seen: bool, menu: Callable[[Match], list[tuple[str, Callable[[], None]]]] | None = None) -> None:
        super().__init__()
        self.rows = JobRows(seen)
        self.menu_for = menu
        self.hover: tuple[int, str | None] = (-1, None)
        self.setModel(self.rows)
        self.setItemDelegate(JobDelegate(self))
        self.setMouseTracking(True)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(False)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._pressed: tuple[int, str | None] = (-1, None)
        # "Found 2:32 PM (12 min. ago)": kept current, once a minute.
        self._clock = QTimer(self, interval=60_000, timeout=self.viewport().update)
        self._clock.start()
        style.text_size.changed.connect(self.doItemsLayout)

    def show_rows(self, rows: list[Match]) -> None:
        """New rows, keeping the list where it was scrolled to."""
        at = self.verticalScrollBar().value()
        self.rows.set_rows(rows)
        self.verticalScrollBar().setValue(at)

    def _part_at(self, point: QPoint) -> tuple[int, str | None]:
        index = self.indexAt(point)
        if not index.isValid():
            return -1, None
        rect = self.visualRect(index).adjusted(px(4), 1, -px(4), -1)
        parts = self.itemDelegate().parts(rect, index.data(ROW))
        for name in ("box", "action"):
            if name in parts and parts[name].contains(point):
                return index.row(), name
        return index.row(), "row"

    def _set_hover(self, hover: tuple[int, str | None]) -> None:
        if hover == self.hover:
            return
        self.hover = hover
        row, part = hover
        link = row >= 0 and part == "row" and bool(self.rows.rows[row].get("url"))
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if link or part == "action" else Qt.CursorShape.ArrowCursor)
        self.viewport().update()

    def mouseMoveEvent(self, event) -> None:
        self._set_hover(self._part_at(event.position().toPoint()))

    def leaveEvent(self, event) -> None:
        self._set_hover((-1, None))

    def mousePressEvent(self, event) -> None:
        self._pressed = self._part_at(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else (-1, None)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        row, part = self._part_at(event.position().toPoint())
        if (row, part) != self._pressed or row < 0:
            return
        match = self.rows.rows[row]
        if part == "box":
            self.rows.toggle(row)
        elif part == "action":
            self.acted.emit(match)
        elif match.get("url"):
            system.open_url(match["url"])

    def mouseDoubleClickEvent(self, event) -> None:
        # A quick second click on a tick box ticks it again; on the row, it doesn't open the posting twice.
        self.mousePressEvent(event)
        if self._pressed[1] != "box":
            self._pressed = (-1, None)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            row, part = self._part_at(event.pos())
            if part == "box":
                self.setToolTip("Select")
            elif part == "action":
                self.setToolTip("Put it back in New jobs")
            else:
                self.setToolTip("Open the posting in your browser" if row >= 0 and self.rows.rows[row].get("url") else "")
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        index = self.currentIndex()
        if index.isValid() and event.key() == Qt.Key.Key_Space:
            self.rows.toggle(index.row())
        elif index.isValid() and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and index.data(ROW).get("url"):
            system.open_url(index.data(ROW)["url"])
        else:
            super().keyPressEvent(event)

    def _menu(self, point: QPoint) -> None:
        index = self.indexAt(point)
        if not index.isValid():
            return
        match: Match = index.data(ROW)
        menu = QMenu(self)
        if match.get("url"):
            menu.addAction("Open Posting", lambda: system.open_url(match["url"]))
            menu.addAction("Copy Link", lambda: QGuiApplication.clipboard().setText(match["url"]))
        extra = self.menu_for(match) if self.menu_for and not match.get("send_at") else []
        if extra:
            menu.addSeparator()
            for title, run in extra:
                menu.addAction(title, run)
        if not menu.isEmpty():
            menu.exec(self.viewport().mapToGlobal(point))
