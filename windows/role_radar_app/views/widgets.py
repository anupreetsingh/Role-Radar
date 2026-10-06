"""The pieces the windows share, each the Mac app's of the same name: a card, an on/off switch and its
row, a status line, a problem said plainly (Trouble), job title chips in a flowing layout, and the ⓘ
beside a heading with its pop-over example."""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QLayout, QLayoutItem, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

from role_radar_app.views import style
from role_radar_app.views.style import MEDIUM, SEMIBOLD, px, sized


def label(text: str = "", size: float = 12, weight=style.QFont.Weight.Normal, tone: str | None = None,
          wrap: bool = True, mono: bool = False, selectable: bool = False) -> QLabel:
    widget = QLabel(text)
    sized(widget, size, weight, mono)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if tone:
        style.tone(widget, tone)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(text: str, action: Callable[[], None] | None = None, size: float = 12, primary: bool = False,
           tip: str = "") -> QPushButton:
    widget = QPushButton(text)
    sized(widget, size)
    if primary:
        widget.setProperty("primary", True)
    if action:
        widget.clicked.connect(lambda *_: action())
    if tip:
        widget.setToolTip(tip)
    return widget


def link(text: str, action: Callable[[], None], size: float = 12) -> QToolButton:
    """A blue text button that opens something."""
    widget = QToolButton()
    widget.setText(text)
    widget.setProperty("link", True)
    widget.setAutoRaise(True)
    sized(widget, size)
    style.hand(widget)
    widget.clicked.connect(lambda *_: action())
    return widget


def row(*widgets: QWidget | None, spacing: int = 8, stretch_at: int | None = None) -> QHBoxLayout:
    """Widgets side by side; `stretch_at` puts the space before that one (-1: after them all)."""
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for index, widget in enumerate(widgets):
        if index == stretch_at:
            layout.addStretch(1)
        if widget is not None:
            layout.addWidget(widget)
    if stretch_at == -1:
        layout.addStretch(1)
    return layout


def column(spacing: int = 8, margins: int = 0) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def clear(layout: QLayout) -> None:
    """Remove and delete everything in `layout`."""
    while (item := layout.takeAt(0)) is not None:
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear(item.layout())


class Card(QFrame):
    """A rounded box of related lines: the Mac app's card."""

    def __init__(self, spacing: int = 8) -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = column(spacing, margins=10)
        self.setLayout(self.body)


# -- busy, and on/off ---------------------------------------------------------------------------


class Spinner(QWidget):
    """Something's working: a small turning arc."""

    def __init__(self, size: float = 14) -> None:
        super().__init__()
        self._size, self._angle = size, 0
        self._timer = QTimer(self, interval=60, timeout=self._turn)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(px(self._size), px(self._size))

    def showEvent(self, event) -> None:
        self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()

    def _turn(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 3
        box = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        painter.setPen(_round_pen(style.secondary(), 2))
        painter.drawArc(box, -self._angle * 16, 270 * 16)


class Switch(QAbstractButton):
    """An on/off switch, as macOS and Windows draw them. `clicked(bool)` is the person flipping it."""

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.active = False  # on and working: green rather than the accent color
        style.hand(self)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(px(36), px(20))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        track = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = track.height() / 2
        on = self.isChecked()
        painter.setPen(Qt.PenStyle.NoPen if on else QPen(style.faded(style.text(), 0.45), 1))
        painter.setBrush((style.GREEN if self.active else style.accent()) if on else Qt.GlobalColor.transparent)
        painter.drawRoundedRect(track, radius, radius)
        knob = track.height() - (6 if on else 8)
        x = track.right() - knob - (3 if on else 4) if on else track.left() + 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("white") if on else style.faded(style.text(), 0.7))
        painter.drawEllipse(QRectF(x, track.center().y() - knob / 2, knob, knob))


ICONS = ("pc", "chat", "mail")


def _round_pen(color: QColor, width: float) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def paint_icon(painter: QPainter, name: str, box: QRectF, color: QColor) -> None:
    """A switch row's icon: "pc" (a laptop), "chat" (Discord) or "mail" (email), drawn in `box`."""
    painter.save()
    painter.setPen(_round_pen(color, max(1.2, box.width() / 11)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    w, h, x, y = box.width(), box.height(), box.x(), box.y()
    if name == "pc":
        painter.drawRoundedRect(QRectF(x + w * 0.18, y + h * 0.22, w * 0.64, h * 0.42), 1.5, 1.5)
        painter.drawLine(QPoint(round(x + w * 0.06), round(y + h * 0.76)), QPoint(round(x + w * 0.94), round(y + h * 0.76)))
    elif name == "chat":
        path = QPainterPath()
        path.addRoundedRect(QRectF(x + w * 0.12, y + h * 0.18, w * 0.76, h * 0.5), 3, 3)
        painter.drawPath(path)
        painter.drawLine(QPoint(round(x + w * 0.32), round(y + h * 0.68)), QPoint(round(x + w * 0.24), round(y + h * 0.84)))
    else:
        envelope = QRectF(x + w * 0.12, y + h * 0.24, w * 0.76, h * 0.52)
        painter.drawRoundedRect(envelope, 1.5, 1.5)
        flap = QPainterPath(envelope.topLeft())
        flap.lineTo(envelope.center().x(), envelope.top() + envelope.height() * 0.55)
        flap.lineTo(envelope.topRight())
        painter.drawPath(flap)
    painter.restore()


class Badge(QWidget):
    """A switch row's round icon: filled when on (green while working), faint when off."""

    def __init__(self, icon: str) -> None:
        super().__init__()
        self.icon, self.on, self.active = icon, False, False
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(px(24), px(24))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        circle = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush((style.GREEN if self.active else style.accent()) if self.on else style.faded(style.text(), 0.14))
        painter.drawEllipse(circle)
        inset = side * 0.2
        paint_icon(painter, self.icon, circle.adjusted(inset, inset, -inset, -inset),
                   QColor("white") if self.on else style.secondary())


class SwitchRow(QWidget):
    """A switch with its icon, name and what it's doing; the Mac app's SwitchRow."""

    flipped = Signal(bool)

    def __init__(self, title: str, icon: str) -> None:
        super().__init__()
        self.badge = Badge(icon)
        self.title = label(title, 13, MEDIUM, wrap=False)
        self.detail = label("", 11, tone="secondary")
        self.action = button("", size=11)
        self.action.hide()
        self.spinner = Spinner()
        self.spinner.hide()
        self.switch = Switch()
        self.switch.setAccessibleName(title)
        self.switch.clicked.connect(lambda *_: self.flipped.emit(self.switch.isChecked()))
        text = column(1)
        text.addWidget(self.title)
        text.addWidget(self.detail)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.badge)
        layout.addLayout(text, 1)
        layout.addWidget(self.spinner)
        layout.addWidget(self.action)
        layout.addWidget(self.switch)
        self._run: Callable[[], None] | None = None
        self.action.clicked.connect(lambda *_: self._run and self._run())

    def show_state(self, detail: str, on: bool, active: bool, busy: bool, locked: bool = False,
                   action: tuple[str, Callable[[], None]] | None = None) -> None:
        self.detail.setText(detail)
        self.badge.on, self.badge.active = on, on and active
        self.badge.update()
        self.switch.setChecked(on)
        self.switch.active = on and active
        self.switch.setEnabled(not busy and not locked)
        self.switch.update()
        self.spinner.setVisible(busy)
        self.action.setVisible(bool(action) and not busy)
        if action:
            self.action.setText(action[0])
            self._run = action[1]


class StatusLine(QWidget):
    """A symbol and a line of secondary text: the round, the jobs waiting, the failing sites."""

    def __init__(self) -> None:
        super().__init__()
        self.symbol = label("", 10, wrap=False, tone="secondary")
        self.symbol.setFixedWidth(px(16))
        self.symbol.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.text = label("", 11, tone="secondary")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.symbol, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text, 1)

    def set(self, text: str, symbol: str, tone: str = "secondary") -> None:
        self.text.setText(text)
        self.symbol.setText(symbol)
        style.tone(self.symbol, tone)


class Trouble(QFrame):
    """Something that went wrong, said plainly, with a way to try again: never a silent blank."""

    def __init__(self, retry: Callable[[], None]) -> None:
        super().__init__()
        self.setObjectName("trouble")
        self.retry = retry
        self.message = label("", 11, mono=True, tone="secondary", selectable=True)
        self.again = button("Try Again", self._try, size=11)
        layout = column(8, margins=10)
        layout.addWidget(label("⚠  Role Radar hit a problem", 12, SEMIBOLD, tone="orange"))
        layout.addWidget(self.message)
        layout.addLayout(row(self.again, stretch_at=-1))
        self.setLayout(layout)
        self.hide()

    def show_message(self, message: str | None) -> None:
        self.message.setText(message or "")
        self.setVisible(bool(message))

    def _try(self) -> None:
        self.again.setText("Trying…")
        self.again.setEnabled(False)
        self.retry()
        QTimer.singleShot(1500, lambda: (self.again.setText("Try Again"), self.again.setEnabled(True)))


# -- job title chips ------------------------------------------------------------------------------


class FlowLayout(QLayout):
    """Lays its widgets out left to right, starting a new row when one would overflow."""

    def __init__(self, spacing: int = 6) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, place: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line = rect.x(), y + line + self._gap, 0
            if place:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y()


class Chip(QAbstractButton):
    """A job title box: ticked, it's one of the titles searched for."""

    def __init__(self, text: str, on: bool = False, size: float = 12) -> None:
        super().__init__()
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(on)
        sized(self, size)
        style.hand(self)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        check = metrics.horizontalAdvance("✓ ") if self.isChecked() else 0
        return QSize(metrics.horizontalAdvance(self.text()) + check + px(20), metrics.height() + px(9))

    def nextCheckState(self) -> None:
        super().nextCheckState()
        self.updateGeometry()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = box.height() / 2
        on = self.isChecked()
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(style.accent())
        else:
            painter.setPen(QPen(style.faded(style.text(), 0.3), 1))
            painter.setBrush(style.faded(style.text(), 0.1 if self.underMouse() else 0.04))
        painter.drawRoundedRect(box, radius, radius)
        painter.setPen(QColor("white") if on else style.text())
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, ("✓ " if on else "") + self.text())

    def enterEvent(self, event) -> None:
        self.update()

    def leaveEvent(self, event) -> None:
        self.update()


def chips(titles: Iterable[str], ticked: set[str] | None, toggled: Callable[[str, bool], None] | None = None,
          size: float = 12) -> QWidget:
    """Chips in a flowing layout; ticking one calls `toggled(title, on)`. Without `toggled`, they're
    only shown (the ⓘ's examples)."""
    holder = QWidget()
    flow = FlowLayout()
    holder.setLayout(flow)
    for title in titles:
        chip = Chip(title, ticked is None or title in ticked, size)
        if toggled:
            chip.toggled.connect(lambda on, t=title: toggled(t, on))
        else:
            chip.setEnabled(False)
            chip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        flow.addWidget(chip)
    return holder


# -- the ⓘ ---------------------------------------------------------------------------------------


class Popover(QWidget):
    """A pop-over beside what opened it, closed by clicking anywhere else."""

    def __init__(self, content: QWidget, width: int) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        frame = QFrame()
        frame.setObjectName("popover")
        inner = column(0, margins=18)
        inner.addWidget(content)
        frame.setLayout(inner)
        outer = column(0)
        outer.addWidget(frame)
        self.setLayout(outer)
        self.setFixedWidth(width)

    def show_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        point = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        screen = (anchor.screen() or QApplication.primaryScreen()).availableGeometry()
        point.setX(max(screen.left() + 8, min(point.x(), screen.right() - self.width() - 8)))
        if point.y() + self.height() > screen.bottom():
            point.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
        self.move(point)
        self.show()


class InfoButton(QToolButton):
    """An ⓘ beside a heading: a hint on hover, the full explanation in a pop-over on click."""

    def __init__(self, hint: str, content: Callable[[], QWidget]) -> None:
        super().__init__()
        self.setText("ⓘ")
        self.setAutoRaise(True)
        self.setToolTip(hint)
        self.setAccessibleName("More about this")
        sized(self, 14)
        style.hand(self)
        self.content = content
        self.clicked.connect(self._open)

    def _open(self) -> None:
        Popover(self.content(), px(380)).show_below(self)


def ticked_example(rule: str, targets: list[str], words: list[str], reach: list[str], stopped: list[str]) -> QWidget:
    """An ⓘ's example: the boxes ticked, drawn as Setup draws them, then the jobs that reach you and those that don't."""
    holder = QWidget()
    layout = column(14)
    holder.setLayout(layout)
    layout.addWidget(label(rule, 13))
    layout.addWidget(label("You have ticked:", 13, SEMIBOLD))
    for name, titles in (("Target roles", targets), ("Non-target roles", words)):
        if titles:
            line = QHBoxLayout()
            line.setSpacing(10)
            line.addWidget(label(name, 12, tone="secondary", wrap=False), 0, Qt.AlignmentFlag.AlignTop)
            line.addWidget(chips(titles, None), 1)
            layout.addLayout(line)
    for heading, titles, good in (("Then, these reach you:", reach, True),
                                  ("Then, these don't:" if not reach else "And these don't:", stopped, False)):
        if not titles:
            continue
        layout.addWidget(label(heading, 13, SEMIBOLD))
        for title in titles:
            mark = label("✓" if good else "✕", 13, style.BOLD, tone="green" if good else "red", wrap=False)
            layout.addLayout(row(mark, label(title, 13), spacing=8))
    return holder
