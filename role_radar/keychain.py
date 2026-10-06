"""Alert settings in this computer's keychain (runtime.secrets: keychain), for running without AWS:
macOS's Keychain, or on Windows its Credential Manager (wincred.py).

Each setting (the names notifications.notifiers_from_env reads) is kept under a service, "role-radar"
by default, with the setting's name as its account. On macOS that's a generic password, read and
written with macOS's `security` tool; on Windows, a generic credential named "<service>/<name>".
The checker started in the background can read them too, which it can't do with environment
variables. The packaged app sets $ROLE_RADAR_KEYCHAIN to a service of its own, apart from one run
from the code.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys

SERVICE = "role-radar"
NAMES = ("EMAIL_TO", "EMAIL_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD",
         "DISCORD_WEBHOOK_URL")
NOT_FOUND = 44  # `security`'s exit status when there's no such item


def service() -> str:
    return os.environ.get("ROLE_RADAR_KEYCHAIN") or SERVICE


def _windows() -> bool:
    return sys.platform == "win32"


def _target(name: str) -> str:
    """A setting's credential on Windows."""
    return f"{service()}/{name}"


def _security(*args: str, **kwargs) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RuntimeError("runtime.secrets: keychain works on macOS and Windows only")
    return subprocess.run(["security", *args], text=True, **kwargs)


def read(name: str) -> str | None:
    """A setting's value, or None if it isn't set."""
    if _windows():
        from role_radar import wincred

        return wincred.read(_target(name))
    done = _security("find-generic-password", "-s", service(), "-a", name, "-w", capture_output=True)
    if done.returncode == NOT_FOUND:
        return None
    if done.returncode:
        raise RuntimeError(f"couldn't read {name} from the Keychain: {done.stderr.strip()}")
    return done.stdout.removesuffix("\n")


def read_all() -> dict[str, str]:
    """Every setting that's set."""
    return {name: value for name in NAMES if (value := read(name)) is not None}


def write(name: str, value: str | None = None) -> None:
    """Set a setting, never putting its value on a command line (where other programs could see it).

    Without `value`, it's asked for on the terminal, twice and hidden. On macOS a value goes to
    `security`'s interactive mode on stdin, and is read back to check.
    """
    if name not in NAMES:
        raise ValueError(f"unknown setting {name!r}; one of {', '.join(NAMES)}")
    if _windows():
        _write_windows(name, value)
        return
    if value is None:
        done = _security("add-generic-password", "-U", "-s", service(), "-a", name, "-l", f"Role Radar {name}", "-w")
        if done.returncode:
            raise RuntimeError(f"couldn't save {name} in the Keychain")
        return
    if not value or any(c in value for c in '"\\\n\r'):
        raise ValueError(f"{name} can't be empty, or contain quotes, backslashes or line breaks")
    command = f'add-generic-password -U -s {service()} -a {name} -l "Role Radar {name}" -w "{value}"\n'
    done = _security("-i", input=command, capture_output=True)
    if done.returncode or read(name) != value:
        raise RuntimeError(f"couldn't save {name} in the Keychain" + (f": {done.stderr.strip()}" if done.stderr else ""))


def _write_windows(name: str, value: str | None) -> None:
    from role_radar import wincred

    if value is None:
        value = getpass.getpass(f"{name}: ")
        if getpass.getpass("Again: ") != value:
            raise ValueError("the two didn't match")
    if not value or any(c in value for c in "\n\r"):
        raise ValueError(f"{name} can't be empty, or contain line breaks")
    wincred.write(_target(name), value, user=name)


def delete(name: str) -> bool:
    """Remove a setting. False if it wasn't set."""
    if _windows():
        from role_radar import wincred

        return wincred.delete(_target(name))
    done = _security("delete-generic-password", "-s", service(), "-a", name, capture_output=True)
    if done.returncode == NOT_FOUND:
        return False
    if done.returncode:
        raise RuntimeError(f"couldn't delete {name} from the Keychain: {done.stderr.strip()}")
    return True
