"""`role-radar login-item on|off`: the Mac's checker, run in the background by launchd.

The menu bar app owns it: opening the app starts it (and restarts it within a
minute if it stops), quitting the app stops it. So the checker runs exactly
while the app does, and quitting and reopening the app restarts it on the
current code. At login it starts with the app (the app's Open at Login).

A launchd LaunchAgent without RunAtLoad or KeepAlive: launchd never starts it
by itself, it only runs it with its own log and background priority when
asked, so it outlives an app crash rather than dying with it.

Its label is $ROLE_RADAR_AGENT (default com.roleradar.start), so the packaged
app's checker is a separate agent, with its own log, from one run from the code.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.roleradar.start"
# Environment worth passing on to the login item, when set where `login-item on` runs.
PASSED_ENV = ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "ROLE_RADAR_HOME")


def label() -> str:
    return os.environ.get("ROLE_RADAR_AGENT") or LABEL


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label()}.plist"


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs"


def log_file() -> Path:
    """The checker's log: role-radar.log, or <label>.log for another agent."""
    return log_dir() / ("role-radar.log" if label() == LABEL else f"{label()}.log")


def build_plist(program: list[str], env: dict[str, str], working_dir: Path) -> dict:
    return {
        "Label": label(),
        "ProgramArguments": program,
        "RunAtLoad": False,  # the menu bar app starts it; deliberately no KeepAlive either
        "ExitTimeOut": 60,  # after SIGTERM, time to finish the companies in flight before launchd kills it
        "ProcessType": "Background",
        "WorkingDirectory": str(working_dir),
        "EnvironmentVariables": env,
        # The app writes its own rotating log (--log-file); this catches crashes only.
        "StandardOutPath": str(log_file().with_suffix(".out.log")),
        "StandardErrorPath": str(log_file().with_suffix(".out.log")),
    }


def passed_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = dict(os.environ if environ is None else environ)
    names = [n for n in environ if n in PASSED_ENV or n.startswith("ROLE_RADAR_")]
    return {n: environ[n] for n in sorted(names)}


def install(program: list[str], env: dict[str, str], working_dir: Path) -> Path:
    """Write the LaunchAgent, load it and start it now."""
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(build_plist(program, env, working_dir), fh)
    _launchctl("bootout", f"{_domain()}/{label()}", check=False)  # reload if it was already on
    _launchctl("bootstrap", _domain(), str(path))
    _launchctl("kickstart", f"{_domain()}/{label()}")
    return path


def start() -> bool:
    """Start the login item's `role-radar start` now (a no-op if it's running). False if the login item isn't on."""
    path = plist_path()
    if not path.exists():
        return False
    if _launchctl("kickstart", f"{_domain()}/{label()}", check=False).returncode != 0:
        _launchctl("bootstrap", _domain(), str(path), check=False)  # not loaded (e.g. after logging in): load it
        _launchctl("kickstart", f"{_domain()}/{label()}", check=False)
    return True


def uninstall() -> bool:
    """Unload the LaunchAgent (stopping the app if launchd started it) and delete it. False if it wasn't on."""
    path = plist_path()
    existed = path.exists()
    _launchctl("bootout", f"{_domain()}/{label()}", check=False)
    path.unlink(missing_ok=True)
    return existed


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=check, capture_output=True, text=True)
