"""Running role-radar commands: `<Python> -m role_radar ARGS --config <the app's companies file>`,
with the app's own files and credentials (place.ENV), as the Mac app's Model.cli does. Each runs
in a worker thread, so the window never waits on one, and its result comes back on the main thread.
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal

from role_radar_app import place

CREATE_NO_WINDOW = 0x08000000  # a dev run's python.exe would flash a console window for each command


@dataclass
class Result:
    ok: bool
    data: bytes = b""
    message: str = ""  # why it failed: the last line role-radar printed to stderr

    def json(self) -> Any:
        """What it printed, as JSON. ValueError if it isn't."""
        return json.loads(self.data)


def command(args: list[str]) -> list[str]:
    return [place.PYTHON, "-m", "role_radar", *args, "--config", str(place.CONFIG)]


def run(args: list[str], stdin: str | None = None) -> Result:
    """Run `role-radar ARGS` and wait for it."""
    place.SUPPORT.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(command(args), input=None if stdin is None else stdin.encode(),
                              stdin=subprocess.DEVNULL if stdin is None else None, capture_output=True,
                              cwd=place.SUPPORT, env={**os.environ, **place.ENV, "PYTHONIOENCODING": "utf-8"},
                              creationflags=CREATE_NO_WINDOW if place.WINDOWS else 0)
    except OSError as error:
        return Result(False, message=f"Couldn't run {place.PYTHON}: {error}")
    if done.returncode == 0:
        return Result(True, done.stdout)
    lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return Result(False, message=lines[-1] if lines else f"exit {done.returncode}")


def start(args: list[str]) -> None:
    """Start `role-radar ARGS` and don't wait: it outlives the app (the stop on quit)."""
    place.SUPPORT.mkdir(parents=True, exist_ok=True)
    flags = 0x00000008 | 0x00000200 if place.WINDOWS else 0  # detached, in a process group of its own
    subprocess.Popen(command(args), cwd=place.SUPPORT, env={**os.environ, **place.ENV}, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                     start_new_session=not place.WINDOWS)


class Calls(QObject):
    """Runs commands in worker threads and hands each result to its callback on the main thread.
    `inline` (the tests) runs them at once instead, so a click's whole effect is there when it returns."""

    _finished = Signal(object, object)  # the callback, the result

    def __init__(self, runner: Callable[[list[str], str | None], Result] = run, inline: bool = False) -> None:
        super().__init__()
        self.runner, self.inline = runner, inline
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="role-radar")
        # A bound method of an object on the main thread: the call is queued to it from a worker.
        self._finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def call(self, args: list[str], then: Callable[[Result], None] | None = None, stdin: str | None = None) -> None:
        if self.inline:
            self._deliver(then, self.runner(args, stdin))
            return
        self._pool.submit(lambda: self._finished.emit(then, self.runner(args, stdin)))

    def _deliver(self, then: Callable[[Result], None] | None, result: Result) -> None:
        if then:
            then(result)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
