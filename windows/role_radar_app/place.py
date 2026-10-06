"""Where the app finds its Python, its files and its settings: the Mac app's `Place`.

Installed (scripts/package_windows.sh), Role Radar.exe runs the app from its own folder, beside
app.json, which names the app, its files' folder in %LOCALAPPDATA%, its Credential Manager service
and its update feed. Without app.json it's a dev run from the repo (scripts/run_windows_app.sh):
"Role Radar Dev", an app of its own (its own files, checker and credentials) that checks no job
sites unless RR_DEV_CHECKS=1, so trying it out never touches an installed Role Radar, nor doubles
the requests this computer makes. On a Mac it's "Role Radar Windows Dev", apart from the Mac's
own dev build.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from role_radar import __version__

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"


def _installed() -> Path | None:
    """The installed app's folder: Role Radar.exe (this Python) is beside app.json."""
    folder = Path(sys.executable).resolve().parent
    return folder if (folder / "app.json").is_file() else None


def _data_dir() -> Path:
    """Where apps keep their files: %LOCALAPPDATA% on Windows."""
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if MAC:
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


INSTALLED = _installed()
INFO: dict = json.loads((INSTALLED / "app.json").read_text(encoding="utf-8")) if INSTALLED else {}
DEV: bool = not INSTALLED or bool(INFO.get("dev"))  # a dev run, or the dev installer (package_windows.sh --dev)
NAME: str = INFO.get("name") or ("Role Radar Dev" if WINDOWS else "Role Radar Windows Dev")
VERSION = __version__ + ("-dev" if DEV else "")
SUPPORT: Path = _data_dir() / (INFO.get("home") or NAME)  # its files: settings, state, logs
KEYCHAIN: str = INFO.get("keychain") or NAME  # its Credential Manager service (the Keychain's, on a Mac)
# Whether it may check job sites: a dev run doesn't (unless RR_DEV_CHECKS=1), so trying it out never
# doubles the requests this computer makes.
CHECKS: bool = bool(INFO["checks"]) if "checks" in INFO else os.environ.get("RR_DEV_CHECKS") == "1"
PYTHON: str = sys.executable  # installed: Role Radar.exe, which runs `-m role_radar ...` as Python would
CONFIG: Path = SUPPORT / "companies.yaml"
FEED: str | None = INFO.get("feed")  # the update feed, with the key updates must be signed with
UPDATE_KEY: str | None = INFO.get("update_key")
# Its checker's files and credentials are its own, apart from a checker run from the code. On a Mac
# (a dev run), its checker is a launchd agent of its own too.
ENV: dict[str, str] = {"ROLE_RADAR_HOME": str(SUPPORT), "ROLE_RADAR_KEYCHAIN": KEYCHAIN,
                       **({"ROLE_RADAR_AGENT": "com.roleradar.windows-dev.checker"} if MAC else {})}


def checker_log() -> Path:
    """The checker's log, which the panel's Log button opens."""
    if MAC:
        return Path.home() / "Library" / "Logs" / f"{ENV['ROLE_RADAR_AGENT']}.log"
    return SUPPORT / "checker.log"


def app_log() -> Path:
    """The app's own log: anything that went wrong in the window or the tray."""
    return SUPPORT / "app.log"
