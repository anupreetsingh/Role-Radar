"""Updates for the installed app, with WinSparkle (https://winsparkle.org), the Windows Sparkle.

app.json names the feed (the latest GitHub release's appcast-windows.xml) and the public key updates
must be signed with: the Mac app's, so scripts/publish_update.sh signs both with one key. WinSparkle
looks for a newer version every six hours, and "Check for Updates…" looks now; it shows its own
windows, downloads the installer, checks its signature, and runs it silently (/S) once the app has
quit. The installer then opens the new version. A dev run, or one without WinSparkle.dll, has no updater.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from role_radar_app import place

log = logging.getLogger(__name__)

CHECK_EVERY = 6 * 3600  # seconds


class Updates(QObject):
    quit_requested = Signal()  # WinSparkle has started the installer: quit so it can replace the app

    def __init__(self) -> None:
        super().__init__()
        self._dll: Any = None
        self._callbacks: list[Any] = []  # kept alive while WinSparkle may call them

    @property
    def available(self) -> bool:
        return self._dll is not None

    def start(self) -> None:
        if self._dll or not place.WINDOWS or place.DEV or not place.FEED or not place.UPDATE_KEY:
            return
        try:
            dll = ctypes.CDLL(str(place.INSTALLED / "WinSparkle.dll"))
        except OSError as error:
            log.warning("No updates: %s", error)
            return
        dll.win_sparkle_set_appcast_url(place.FEED.encode())
        if not dll.win_sparkle_set_eddsa_public_key(place.UPDATE_KEY.encode()):
            log.warning("No updates: WinSparkle refused the update key")
            return
        dll.win_sparkle_set_app_details.argtypes = [ctypes.c_wchar_p] * 3
        dll.win_sparkle_set_app_details("Role Radar", place.NAME, place.VERSION)
        dll.win_sparkle_set_automatic_check_for_updates(1)  # without asking first: the Mac app doesn't either
        dll.win_sparkle_set_update_check_interval(CHECK_EVERY)
        # Called from WinSparkle's own thread: say yes, then quit on the main thread (a queued signal).
        can_quit = ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 1)
        quit_now = ctypes.CFUNCTYPE(None)(self.quit_requested.emit)
        self._callbacks = [can_quit, quit_now]
        dll.win_sparkle_set_can_shutdown_callback(can_quit)
        dll.win_sparkle_set_shutdown_request_callback(quit_now)
        dll.win_sparkle_init()
        self._dll = dll

    def check(self) -> None:
        """Look for an update now, showing WinSparkle's window either way."""
        if self._dll:
            self._dll.win_sparkle_check_update_with_ui()

    def stop(self) -> None:
        if self._dll:
            self._dll.win_sparkle_cleanup()
            self._dll = None
