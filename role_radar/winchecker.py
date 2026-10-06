"""The Windows checker: `role-radar start` as a background process of its own.

What launchd does on macOS (launchd.py). The Windows app asks for it with `role-radar switch --start`
(as the Mac app does), which starts it with no window, below normal priority, and apart from the app,
so an app crash doesn't stop it. `role-radar stop` asks it to finish the companies in flight and quit,
which the app does when it quits. Its log is checker.log in Role Radar's home ($ROLE_RADAR_HOME), and
anything it prints before that's open (a crash at startup) goes to checker.out.log beside it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from role_radar.instance import home

# Process creation flags (subprocess has names for them only on Windows).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000  # not ended with a job the app runs in (some launchers' jobs end their children)
FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS


def log_file() -> Path:
    return home() / "checker.log"


def program(config: Path) -> list[str]:
    """The checker's command line: the Python running this, without a console window."""
    return [_windowless(sys.executable), "-m", "role_radar", "start", "--config", str(config), "--log-file", str(log_file())]


def start(config: Path) -> None:
    """Start the checker now, on its own. The caller checks it isn't running already (InstanceLock)."""
    log_file().parent.mkdir(parents=True, exist_ok=True)
    with log_file().with_name("checker.out.log").open("ab") as out:
        options = dict(cwd=str(config.parent), stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True)
        try:
            subprocess.Popen(program(config), creationflags=FLAGS | CREATE_BREAKAWAY_FROM_JOB, **options)
        except OSError:  # running in a job that doesn't allow breaking away
            subprocess.Popen(program(config), creationflags=FLAGS, **options)


def _windowless(python: str) -> str:
    """python.exe opens a console window; pythonw.exe beside it doesn't. The app's own Role Radar.exe never does."""
    path = Path(python)
    if path.name.lower() == "python.exe" and (quiet := path.with_name("pythonw.exe")).exists():
        return str(quiet)
    return python
