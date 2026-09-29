"""Alert settings in this Mac's Keychain (runtime.secrets: keychain), for running without AWS.

Each setting (the names notifications.notifiers_from_env reads) is a generic
password with service "role-radar" and the setting's name as its account,
read and written with macOS's `security` tool. The checker started by launchd
can read them too, which it can't do with environment variables.
"""

from __future__ import annotations

import subprocess
import sys

SERVICE = "role-radar"
NAMES = ("EMAIL_TO", "EMAIL_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD",
         "DISCORD_WEBHOOK_URL")
NOT_FOUND = 44  # `security`'s exit status when there's no such item


def _security(*args: str, **kwargs) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RuntimeError("runtime.secrets: keychain works on macOS only")
    return subprocess.run(["security", *args], text=True, **kwargs)


def read(name: str) -> str | None:
    """A setting's value, or None if it isn't set."""
    done = _security("find-generic-password", "-s", SERVICE, "-a", name, "-w", capture_output=True)
    if done.returncode == NOT_FOUND:
        return None
    if done.returncode:
        raise RuntimeError(f"couldn't read {name} from the Keychain: {done.stderr.strip()}")
    return done.stdout.removesuffix("\n")


def read_all() -> dict[str, str]:
    """Every setting that's set."""
    return {name: value for name in NAMES if (value := read(name)) is not None}


def write(name: str, value: str | None = None) -> None:
    """Set a setting. Without `value`, `security` asks for it on the terminal (twice, hidden), so it's never on a command line."""
    if name not in NAMES:
        raise ValueError(f"unknown setting {name!r}; one of {', '.join(NAMES)}")
    args = ["add-generic-password", "-U", "-s", SERVICE, "-a", name, "-l", f"Role Radar {name}", "-w"]
    done = _security(*args, *([value] if value is not None else []), capture_output=value is not None)
    if done.returncode:
        raise RuntimeError(f"couldn't save {name} in the Keychain" + (f": {done.stderr.strip()}" if done.stderr else ""))


def delete(name: str) -> bool:
    """Remove a setting. False if it wasn't set."""
    done = _security("delete-generic-password", "-s", SERVICE, "-a", name, capture_output=True)
    if done.returncode == NOT_FOUND:
        return False
    if done.returncode:
        raise RuntimeError(f"couldn't delete {name} from the Keychain: {done.stderr.strip()}")
    return True
