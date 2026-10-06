"""One `role-radar start` per machine, and a way for `role-radar stop` to find it.

The running instance holds an exclusive lock on a PID file: flock on macOS and Linux, a locked
byte (msvcrt) on Windows. The OS drops the lock when the process exits, however it exits, so a
crash never leaves a stale lock behind.

`stop` asks it to quit with SIGTERM. Windows has no such signal, so there it leaves a stop file
beside the PID file instead, which the running instance watches for (cli._handle_signals).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    # The byte locked: past the PID, so other processes can still read that (a Windows lock is mandatory).
    _LOCKED_BYTE = 1 << 20

    def _lock(fd: int, exclusive: bool) -> bool:  # noqa: ARG001 (Windows' locks are all exclusive)
        os.lseek(fd, _LOCKED_BYTE, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCKED_BYTE, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int, exclusive: bool) -> bool:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def home() -> Path:
    """Where Role Radar keeps its local files (ROLE_RADAR_HOME, default ~/.role-radar)."""
    return Path(os.environ.get("ROLE_RADAR_HOME") or Path.home() / ".role-radar").expanduser()


class InstanceLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or home() / "start.pid"
        self._fd: int | None = None

    @property
    def stop_file(self) -> Path:
        """Where `stop` asks the running instance to quit, on Windows."""
        return self.path.with_suffix(".stop")

    def acquire(self) -> bool:
        """Become the running instance. False if another one already is."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        if not _lock(fd, exclusive=True):
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        self.stop_file.unlink(missing_ok=True)  # a request left from before isn't for this instance
        return True

    def release(self) -> None:
        if self._fd is not None:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = None

    def running_pid(self) -> int | None:
        """The running instance's PID, or None if no instance is running."""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return None
        try:
            if _lock(fd, exclusive=False):
                _unlock(fd)
                return None
            os.lseek(fd, 0, os.SEEK_SET)  # held, so an instance is running
            text = os.read(fd, 32).decode().strip()
            return int(text) if text.isdigit() else None
        finally:
            os.close(fd)

    def stop_running(self, timeout: float = 90.0) -> int | None:
        """Ask the running instance to quit and wait for it. Returns its PID, or None if none ran."""
        pid = self.running_pid()
        if pid is None:
            return None
        if sys.platform == "win32":
            self.stop_file.touch()
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while self.running_pid() == pid and time.monotonic() < deadline:
            time.sleep(0.25)
        if self.running_pid() == pid:
            raise TimeoutError(f"role-radar (pid {pid}) is still finishing after {timeout:.0f}s")
        return pid

    def stop_requested(self) -> bool:
        """Whether `stop` has asked this instance to quit (on Windows), taking the request."""
        try:
            self.stop_file.unlink()
        except FileNotFoundError:
            return False
        return True
