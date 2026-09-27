"""`role-radar login-item on|off`: start the monitor when you log in to your Mac.

A launchd LaunchAgent with RunAtLoad and no KeepAlive: launchd starts
`role-radar start` at login (and when the item is turned on), but never
restarts it, so quitting it (`role-radar stop`) stays quit until the next
login.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.roleradar.start"
# Environment worth passing on to the login item, when set where `login-item on` runs.
PASSED_ENV = ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "ROLE_RADAR_HOME")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs"


def build_plist(program: list[str], env: dict[str, str], working_dir: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": program,
        "RunAtLoad": True,  # at login; there's deliberately no KeepAlive
        "ExitTimeOut": 60,  # after SIGTERM, time to finish the companies in flight before launchd kills it
        "ProcessType": "Background",
        "WorkingDirectory": str(working_dir),
        "EnvironmentVariables": env,
        # The app writes its own rotating log (--log-file); this catches crashes only.
        "StandardOutPath": str(log_dir() / "role-radar.out.log"),
        "StandardErrorPath": str(log_dir() / "role-radar.out.log"),
    }


def passed_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = dict(os.environ if environ is None else environ)
    names = [n for n in environ if n in PASSED_ENV or n.startswith("ROLE_RADAR_")]
    return {n: environ[n] for n in sorted(names)}


def install(program: list[str], env: dict[str, str], working_dir: Path) -> Path:
    """Write the LaunchAgent and load it, which also starts it now."""
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(build_plist(program, env, working_dir), fh)
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)  # reload if it was already on
    _launchctl("bootstrap", _domain(), str(path))
    return path


def uninstall() -> bool:
    """Unload the LaunchAgent (stopping the app if launchd started it) and delete it. False if it wasn't on."""
    path = plist_path()
    existed = path.exists()
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    path.unlink(missing_ok=True)
    return existed


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=check, capture_output=True, text=True)
