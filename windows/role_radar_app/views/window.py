"""The app's one window: Setup's pages until they're done (or when asked for again), otherwise Live
Tracking, switching in place rather than opening another window. Closing it leaves the tray icon,
still checking. Its toolbar has the way between the two, and smaller and bigger text.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMainWindow, QSizePolicy, QStackedWidget, QToolBar, QWidget

from role_radar_app import place
from role_radar_app.model import Model
from role_radar_app.views import icons
from role_radar_app.views.live import LiveView
from role_radar_app.views.setup import SetupView
from role_radar_app.views.style import px, sized, text_size


class MainWindow(QMainWindow):
    def __init__(self, model: Model) -> None:
        super().__init__()
        self.model = model
        self.setWindowIcon(icons.app_icon())
        self.setup_view = SetupView(model)
        self.live_view = LiveView(model)
        self.setup_view.finished.connect(lambda: self.show_page(setup=False))
        self.pages = QStackedWidget()
        self.pages.addWidget(self.setup_view)
        self.pages.addWidget(self.live_view)
        self.setCentralWidget(self.pages)
        self._toolbar()
        self.resize(px(1000), px(780))
        model.setup_changed.connect(self._show_toolbar)
        model.window_wanted.connect(self.bring_up)

    def _toolbar(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.setIconSize(QSize(16, 16))
        self.to_setup = QAction("⚙  Edit Setup", self)
        self.to_setup.setToolTip("Change your profession, countries, roles, qualifications or alerts")
        self.to_setup.triggered.connect(lambda: self.show_page(setup=True))
        self.to_live = QAction("☰  Live Tracking", self)
        self.to_live.setToolTip("Back to the new jobs; what you changed here is saved")
        self.to_live.triggered.connect(lambda: self.show_page(setup=False))
        bar.addAction(self.to_setup)
        bar.addAction(self.to_live)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.smaller = QAction("A−", self, shortcut=QKeySequence.StandardKey.ZoomOut, triggered=lambda: text_size.change(-0.1))
        self.smaller.setToolTip("Smaller text (Ctrl+−)")
        self.bigger = QAction("A+", self, shortcut=QKeySequence("Ctrl+="), triggered=lambda: text_size.change(0.1))
        self.bigger.setToolTip("Bigger text (Ctrl+=)")
        standard = QAction("Standard Text Size", self, shortcut=QKeySequence("Ctrl+0"), triggered=text_size.reset)
        bigger_too = QAction(self, shortcut=QKeySequence.StandardKey.ZoomIn, triggered=lambda: text_size.change(0.1))
        self.addActions([standard, bigger_too])
        bar.addAction(self.smaller)
        bar.addAction(self.bigger)
        for action in (self.to_setup, self.to_live, self.smaller, self.bigger):
            sized(bar.widgetForAction(action), 12)
        text_size.changed.connect(self._show_toolbar)
        self.addToolBar(bar)
        self._show_toolbar()

    def _show_toolbar(self) -> None:
        setup = self.pages.currentWidget() is self.setup_view
        self.to_setup.setVisible(not setup)
        self.to_live.setVisible(setup and self.model.ready)
        self.smaller.setEnabled(text_size.scale > text_size.LOW)
        self.bigger.setEnabled(text_size.scale < text_size.HIGH)

    def show_page(self, setup: bool) -> None:
        if not setup and self.pages.currentWidget() is self.setup_view:
            self.setup_view.save_if_changed()
        self.model.showing_setup = setup
        self.pages.setCurrentWidget(self.setup_view if setup else self.live_view)
        self.setWindowTitle(("Role Radar Setup" if setup else "Live Tracking") + (" (dev)" if place.DEV else ""))
        self._show_toolbar()

    def bring_up(self) -> None:
        """Open the window, in front, on the page the model asks for."""
        self.show_page(self.model.showing_setup or not self.model.ready)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        """Closing it keeps what was changed in Setup, and leaves the tray icon, still checking."""
        if self.pages.currentWidget() is self.setup_view:
            self.setup_view.save_if_changed()
        event.ignore()
        self.hide()
