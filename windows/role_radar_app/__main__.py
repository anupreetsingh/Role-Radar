"""Start the Windows app: `Role Radar.exe` (installed) or `python -m role_radar_app` (a dev run).

  --login   opened at login: start in the tray only, unless Setup isn't done
  --quit    ask a running Role Radar to quit, and stop its checker (the installer, before an update)

Opening it while it runs shows the running one's window instead.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from role_radar_app import cli, place, system
from role_radar_app.model import Model
from role_radar_app.updates import Updates
from role_radar_app.views import icons, style
from role_radar_app.views.panel import Panel
from role_radar_app.views.tray import Tray
from role_radar_app.views.window import MainWindow

log = logging.getLogger("role_radar_app")


def _log_to_file() -> None:
    """The app's own log (app.log, beside its files): a crash in the window or the tray says what happened."""
    place.SUPPORT.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(place.app_log(), maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    sys.excepthook = lambda kind, error, trace: log.critical("Unexpected error", exc_info=(kind, error, trace))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(place.NAME)
    app.setApplicationDisplayName(place.NAME)
    app.setWindowIcon(icons.app_icon())
    app.setQuitOnLastWindowClosed(False)  # closing the window leaves the tray icon, still checking

    instance = system.SingleInstance()
    if "--quit" in argv:
        instance.tell_running("quit")
        cli.run(["stop"])  # and wait for the checker to finish the companies in flight
        return 0
    if instance.tell_running("show"):
        return 0  # opened again: the running one shows its window
    instance.listen()

    _log_to_file()
    log.info("Role Radar %s starting (%s)", place.VERSION, "at login" if system.LOGIN_FLAG in argv else "opened")
    style.install(app)
    calls = cli.Calls()
    model = Model(calls, launched_at_login=system.LOGIN_FLAG in argv)
    window = MainWindow(model)
    updates = Updates()
    updates.start()

    def open_window(setup: bool) -> None:
        model.show_window(setup=setup)

    def quit_app() -> None:
        app.quit()

    panel = Panel(model, open_window, updates.check if updates.available else None, quit_app)
    tray = Tray(model, panel, open_window, updates.check if updates.available else None, quit_app)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()

    # Opened again: its window, on Setup until that's done, otherwise as it was.
    instance.message.connect(lambda message: quit_app() if message == "quit"
                             else model.show_window(setup=True if not model.ready else None))
    updates.quit_requested.connect(app.quit, Qt.ConnectionType.QueuedConnection)  # asked from WinSparkle's thread

    def stopping() -> None:
        """Quit (or signing out, or an update) stops the checker with the app."""
        log.info("Quitting")
        model.stop_checker()
        updates.stop()
        calls.shutdown()
        tray.hide()

    app.aboutToQuit.connect(stopping)
    model.start()
    _ = window  # kept for the app's lifetime
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
