"""The tray icon (the notification area, by the clock): the Mac app's menu bar icon. Gray while nothing
is checking. Click it for the panel; right-click for its menu; double-click for the window."""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from role_radar_app import describe, place, system
from role_radar_app.model import Model
from role_radar_app.views import icons
from role_radar_app.views.panel import Panel


class Tray(QSystemTrayIcon):
    def __init__(self, model: Model, panel: Panel, open_window: Callable[[bool], None],
                 check_updates: Callable[[], None] | None, quit_app: Callable[[], None]) -> None:
        super().__init__(icons.app_icon(gray=True))
        self.model, self.panel = model, panel
        menu = QMenu()
        menu.addAction("Open Role Radar", lambda: open_window(not model.ready))
        menu.addAction("Edit Setup…", lambda: open_window(True))
        menu.addSeparator()
        if check_updates:
            menu.addAction("Check for Updates…", check_updates)
        menu.addAction("Open Log", lambda: system.open_file(place.checker_log()))
        menu.addSeparator()
        menu.addAction("Quit Role Radar", quit_app)
        self._menu = menu  # kept: the tray holds it only by reference
        self.setContextMenu(menu)
        self.activated.connect(self._clicked)
        model.state_changed.connect(self._show)
        self._open_window = open_window
        self._show()

    def _clicked(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.panel.isVisible():
                self.panel.hide()
            else:
                self.panel.show_at(self.geometry())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.panel.hide()
            self._open_window(not self.model.ready)

    def _show(self) -> None:
        state, checking = self.model.state, self.model.checking
        self.setIcon(icons.app_icon(gray=checking is None))
        if checking == "laptop":
            tip = f"Role Radar: {describe.WHO.lower()} is checking sites"
        elif state is not None:
            tip = "Role Radar: nothing is checking sites"
        else:
            tip = "Role Radar"
        self.setToolTip(tip + (" (dev)" if place.DEV else ""))
