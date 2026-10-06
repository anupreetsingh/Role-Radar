"""Checks per hour for the last 24 hours, and the day's totals: the Mac app's ActivityView, with this
PC as the one runner (the Windows app has no Lambda)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QGridLayout, QWidget

from role_radar_app import describe
from role_radar_app.views import style
from role_radar_app.views.style import SEMIBOLD, px
from role_radar_app.views.widgets import column, label, row


def _hour_label(hour: int) -> str:
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


class Bars(QWidget):
    """The bars, one an hour; hovering one picks it out."""

    hovered = Signal(object)  # the hour under the pointer, or None

    def __init__(self) -> None:
        super().__init__()
        self.hours: list[dict[str, Any]] = []
        self.empty = True
        self.at: int | None = None
        self.setMouseTracking(True)
        self.setAccessibleName("Checks per hour over the last 24 hours, by this PC")

    def sizeHint(self) -> QSize:
        return QSize(px(280), px(90))

    def _plot(self) -> QRectF:
        axis = QFontMetrics(style.font(10)).horizontalAdvance("8,888") + px(6)
        return QRectF(axis, px(4), max(1, self.width() - axis - 2), max(1, self.height() - px(4) - px(18)))

    def _top(self) -> int:
        """The y axis' top: a round number at or above the busiest hour."""
        peak = max((h.get("mac", 0) for h in self.hours), default=0)
        return next(top for exponent in range(10) for top in (10**exponent, 2 * 10**exponent, 5 * 10**exponent) if top >= peak)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot, top = self._plot(), self._top()
        painter.setFont(style.font(10))
        for value in (0, top // 2, top) if top > 1 else (0, top):
            y = plot.bottom() - plot.height() * value / top
            painter.setPen(QPen(style.faded(style.text(), 0.18), 1))
            painter.drawLine(plot.left(), round(y), plot.right(), round(y))
            painter.setPen(style.secondary())
            painter.drawText(QRectF(0, y - 8, plot.left() - px(4), 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             describe.number(value))
        count = max(len(self.hours), 1)
        slot = plot.width() / count
        color = style.CHART[style.dark()]
        for index, hour in enumerate(self.hours):
            height = plot.height() * hour.get("mac", 0) / top
            painter.setOpacity(1 if self.at is None or self.at == index else 0.35)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            bar = QRectF(plot.left() + slot * index + slot * 0.14, plot.bottom() - height, slot * 0.72, height)
            painter.drawRoundedRect(bar, 1.5, 1.5)
            when = describe.date(hour.get("start"))
            if when and when.hour % 6 == 0:
                painter.setOpacity(1)
                painter.setPen(style.secondary())
                painter.drawText(QRectF(bar.center().x() - 30, plot.bottom() + 3, 60, px(14)), Qt.AlignmentFlag.AlignCenter,
                                 _hour_label(when.hour))
        if self.empty:
            painter.setOpacity(1)
            painter.setPen(style.secondary())
            painter.setFont(style.font(11))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No checks in the last 24 hours")

    def mouseMoveEvent(self, event) -> None:
        plot = self._plot()
        x = event.position().x()
        at = int((x - plot.left()) / (plot.width() / max(len(self.hours), 1))) if plot.left() <= x <= plot.right() else None
        at = at if at is not None and 0 <= at < len(self.hours) else None
        if at != self.at:
            self.at = at
            self.update()
            self.hovered.emit(self.hours[at] if at is not None else None)

    def leaveEvent(self, event) -> None:
        self.at = None
        self.update()
        self.hovered.emit(None)


class ActivityView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.bars = Bars()
        self.caption = label("", 11, tone="secondary")
        self.bars.hovered.connect(self._caption)
        layout = column(8)
        self.setLayout(layout)
        swatch = QWidget()
        swatch.setFixedSize(px(8), px(8))
        swatch.setStyleSheet(f"background: {style.CHART[style.dark()].name()}; border-radius: 2px;")
        layout.addLayout(row(label("Activity", 12, SEMIBOLD, wrap=False), swatch,
                             label(describe.WHO, 11, tone="secondary", wrap=False), stretch_at=1, spacing=4))
        layout.addWidget(self.bars)
        layout.addWidget(self.caption)
        stats = QGridLayout()
        stats.setContentsMargins(0, 2, 0, 2)
        self.stats: dict[str, Any] = {}
        for column_at, (key, name) in enumerate((("checked", "checks"), ("new_jobs", "new jobs"), ("matches", "matches"),
                                                 ("alerts", "alerts sent"))):
            value = label("0", 15, SEMIBOLD, wrap=False)
            stats.addWidget(value, 0, column_at)
            stats.addWidget(label(name, 10, tone="secondary", wrap=False), 1, column_at)
            self.stats[key] = value
        layout.addLayout(stats)
        self._caption(None)

    def show_activity(self, activity: dict[str, Any]) -> None:
        self.bars.hours = activity.get("hours") or []
        self.bars.empty = not activity.get("checked")
        self.bars.update()
        for key, value in self.stats.items():
            value.setText(describe.compact(activity.get(key) or 0))

    def _caption(self, hour: dict[str, Any] | None) -> None:
        when = describe.date(hour.get("start")) if hour else None
        self.caption.setText(f"{_hour_label(when.hour)}: {describe.WHO} {describe.number(hour.get('mac', 0))} checks"
                             if hour and when else "Checks per hour · hover a bar for details")
