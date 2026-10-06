"""What the app asks of Windows: opening at login, being one app however often it's opened, and
opening links and files. Elsewhere (a dev run on a Mac) opening at login does nothing.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from role_radar_app import place

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # what opens at login, for this user
LOGIN_FLAG = "--login"  # how the app knows it was opened at login, to start in the tray only


def _run_value() -> str:
    return place.NAME


def open_at_login() -> bool:
    if not place.WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, _run_value())
        return True
    except OSError:
        return False


def set_open_at_login(on: bool) -> None:
    """Open (in the tray) at every login, or not. An installed app only: a dev run never opens at login."""
    if not place.WINDOWS or not place.INSTALLED:
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, _run_value(), 0, winreg.REG_SZ, f'"{sys.executable}" {LOGIN_FLAG}')
        else:
            try:
                winreg.DeleteValue(key, _run_value())
            except FileNotFoundError:
                pass


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def open_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)  # the log, before the checker's first line: an empty file, not an error
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class SingleInstance(QObject):
    """One app per user: opening it again (the Start menu, its shortcut) shows the running one's window
    instead, and `Role Radar.exe --quit` (the installer, before an update) asks the running one to quit."""

    message = Signal(str)  # "show" or "quit"

    def __init__(self) -> None:
        super().__init__()
        # A pipe on Windows, which every user's apps share: so the name says whose it is.
        self.name = f"role-radar-{place.SUPPORT.name}-{getpass.getuser()}".replace(" ", "-").lower()
        self._server: QLocalServer | None = None

    def tell_running(self, message: str) -> bool:
        """Send the running app a message. False if none is running."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(1000):
            return False
        if place.WINDOWS:  # Windows lets the program just opened bring a window forward; pass that on
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # type: ignore[attr-defined]
        socket.write(message.encode() + b"\n")
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        return True

    def listen(self) -> None:
        """Become the running app, taking messages from later ones."""
        QLocalServer.removeServer(self.name)  # one left by a crash
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._server.listen(self.name)

    def _accept(self) -> None:
        while self._server and (socket := self._server.nextPendingConnection()):
            socket.readyRead.connect(lambda s=socket: self._read(s))

    def _read(self, socket: QLocalSocket) -> None:
        while socket.canReadLine():
            text = bytes(socket.readLine().data()).decode(errors="replace").strip()
            if text:
                self.message.emit(text)
