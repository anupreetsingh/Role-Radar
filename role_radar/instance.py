"""One `role-radar start` per machine, and a way for `role-radar stop` to find it.

The running instance holds an exclusive flock on a PID file. The OS drops the
lock when the process exits, however it exits, so a crash never leaves a
stale lock behind.
"""

from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path


def home() -> Path:
    """Where Role Radar keeps its local files (ROLE_RADAR_HOME, default ~/.role-radar)."""
    return Path(os.environ.get("ROLE_RADAR_HOME") or Path.home() / ".role-radar").expanduser()


class InstanceLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or home() / "start.pid"
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Become the running instance. False if another one already is."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def running_pid(self) -> int | None:
        """The running instance's PID, or None if no instance is running."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:  # held, so an instance is running
                text = os.read(fd, 32).decode().strip()
                return int(text) if text.isdigit() else None
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)

    def stop_running(self, timeout: float = 90.0) -> int | None:
        """Ask the running instance to quit (SIGTERM) and wait for it. Returns its PID, or None if none ran."""
        pid = self.running_pid()
        if pid is None:
            return None
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while self.running_pid() == pid and time.monotonic() < deadline:
            time.sleep(0.25)
        if self.running_pid() == pid:
            raise TimeoutError(f"role-radar (pid {pid}) is still finishing after {timeout:.0f}s")
        return pid
