"""The app's look: text sizes, colors and the shared style sheet.

Sizes are the Mac app's, in pixels, times the text size setting: it starts at STANDARD whenever the
app opens, and A−/A+ in the window's toolbar and Ctrl+= / Ctrl+− / Ctrl+0 change it until the app quits.
A widget sized with `sized()` follows it. Colors come from the system's palette, so the app is light
or dark as Windows is.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from role_radar_app import place

GREEN = QColor("#2E9E44")
ORANGE = QColor("#D9822B")
RED = QColor("#D13438")
# The activity chart's bars: categorical slot 1, stepped for light or dark.
CHART = {False: QColor("#2A78D6"), True: QColor("#3987E5")}


class TextSize(QObject):
    STANDARD = 1.1
    LOW, HIGH = 0.85, 1.75
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.scale = self.STANDARD

    def change(self, step: float) -> None:
        self._set(round(min(max(self.scale + step, self.LOW), self.HIGH), 2))

    def reset(self) -> None:
        self._set(self.STANDARD)

    def _set(self, scale: float) -> None:
        if scale != self.scale:
            self.scale = scale
            self.changed.emit()


text_size = TextSize()


def px(size: float) -> int:
    """`size` (the Mac app's points) in this app's pixels, at the current text size."""
    return max(8, round(size * text_size.scale))


def font(size: float, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QFont:
    chosen = QFont(QApplication.font())
    if mono:
        chosen.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "Courier New"])
    chosen.setPixelSize(px(size))
    chosen.setWeight(weight)
    return chosen


def sized(widget: QWidget, size: float, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QWidget:
    """Give `widget` text of `size`, following the text size setting."""
    widget.setProperty("rr_font", (size, weight, mono))
    widget.setFont(font(size, weight, mono))
    return widget


def _resize_all() -> None:
    for widget in QApplication.allWidgets():
        spec = widget.property("rr_font")
        if spec:
            widget.setFont(font(*spec))


text_size.changed.connect(_resize_all)

SEMIBOLD, MEDIUM, BOLD = QFont.Weight.DemiBold, QFont.Weight.Medium, QFont.Weight.Bold


# -- colors --------------------------------------------------------------------------------------


def palette() -> QPalette:
    return QApplication.palette()


def dark() -> bool:
    return palette().color(QPalette.ColorRole.Window).lightness() < 128


def text() -> QColor:
    return palette().color(QPalette.ColorRole.WindowText)


def accent() -> QColor:
    role = getattr(QPalette.ColorRole, "Accent", QPalette.ColorRole.Highlight)
    return palette().color(role)


def faded(color: QColor, alpha: float) -> QColor:
    faint = QColor(color)
    faint.setAlphaF(alpha)
    return faint


def secondary() -> QColor:
    """Secondary text: the Mac app's .secondary."""
    return faded(text(), 0.62)


def tertiary() -> QColor:
    return faded(text(), 0.42)


def css(color: QColor) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"


def style_sheet() -> str:
    """The rules every window shares, for the current palette."""
    return f"""
        QFrame#card {{ background: {css(faded(text(), 0.05))}; border-radius: 10px; }}
        QFrame#trouble {{ background: {css(faded(ORANGE, 0.14))}; border-radius: 10px; }}
        QFrame#popover {{ background: {css(palette().color(QPalette.ColorRole.Window))};
                          border: 1px solid {css(faded(text(), 0.18))}; border-radius: 10px; }}
        QLabel[tone="secondary"] {{ color: {css(secondary())}; }}
        QLabel[tone="tertiary"] {{ color: {css(tertiary())}; }}
        QLabel[tone="green"] {{ color: {GREEN.name()}; }}
        QLabel[tone="orange"] {{ color: {ORANGE.name()}; }}
        QLabel[tone="red"] {{ color: {RED.name()}; }}
        QLabel[tone="accent"] {{ color: {accent().name()}; }}
        QPushButton[primary="true"] {{ background: {accent().name()}; color: white; border: none;
                                       border-radius: 5px; padding: 7px 18px; }}
        QPushButton[primary="true"]:hover {{ background: {accent().lighter(110).name()}; }}
        QPushButton[primary="true"]:pressed {{ background: {accent().darker(110).name()}; }}
        QPushButton[primary="true"]:disabled {{ background: {css(faded(text(), 0.12))}; color: {css(tertiary())}; }}
        QPushButton[flat="true"] {{ border: none; background: transparent; text-align: left; padding: 2px 4px; }}
        QPushButton[flat="true"]:hover {{ background: {css(faded(text(), 0.07))}; border-radius: 5px; }}
        QToolButton[link="true"] {{ border: none; background: transparent; color: {accent().name()}; padding: 0; }}
        QToolButton[link="true"]:hover {{ text-decoration: underline; }}
        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
    """


def install(app: QApplication) -> None:
    """The app's font and style sheet, kept in step with Windows' light or dark mode."""
    if place.WINDOWS:
        base = QFont(app.font())
        base.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        app.setFont(base)
    app.setStyleSheet(style_sheet())
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda *_: app.setStyleSheet(style_sheet()))


def tone(widget: QWidget, name: str | None) -> QWidget:
    """Color a label: "secondary", "tertiary", "green", "orange", "red" or "accent"."""
    if widget.property("tone") != name:
        widget.setProperty("tone", name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    return widget


def hand(widget: QWidget) -> QWidget:
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget
