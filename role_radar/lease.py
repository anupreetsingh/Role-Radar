"""The lease: which runner (the laptop or Lambda) may check companies right now.

One record in the store says who holds the lease and until when. A runner
takes it with a conditional write that succeeds only if the lease has expired
or already belongs to that runner, so only one runner works at a time.

Every successful acquisition bumps the lease's `epoch`, and the epoch is the
fencing token. A company's state is committed only if the lease still has the
epoch this runner acquired (the store checks that in the same transaction). So
a runner that was paused past its expiry, such as a laptop that slept mid-run,
can't overwrite what the new holder did, even before it notices it lost the
lease. Before sending alerts, a runner also re-reads the lease.

Expiry uses wall-clock time (time.time), not a monotonic clock. On macOS the
monotonic clock stops while the machine sleeps, so after waking only the wall
clock shows how long the process was away.
"""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


class LeaseLost(Exception):
    """This runner no longer holds the lease: stop without saving or sending anything."""


@dataclass
class LeaseInfo:
    """The lease record as stored."""

    holder: str | None = None
    epoch: int = 0
    expires_at: float = 0.0  # Unix time
    acquired_at: str | None = None
    renewed_at: str | None = None
    released_at: str | None = None
    requested_by: str | None = None  # another runner asking for a handoff
    requested_at: float | None = None

    def held(self, now: float) -> bool:
        return bool(self.holder) and self.expires_at > now


class Lease(ABC):
    """One runner's handle on the lease.

    `check()` uses only what this runner already knows (no I/O). `verify()`
    re-reads the stored lease. Subclasses implement the store round-trips.
    """

    # Stop counting the lease as held this many seconds before it expires, to
    # allow for clock differences between machines and for the save itself.
    MARGIN = 15.0

    def __init__(self, holder: str, clock: Callable[[], float] = time.time) -> None:
        self.holder = holder
        self.clock = clock
        self.epoch: int | None = None  # set while held
        self.expires_at = 0.0

    @property
    def held(self) -> bool:
        return self.epoch is not None and self.clock() < self.expires_at - self.MARGIN

    def check(self) -> None:
        """Raise LeaseLost unless this runner holds the lease, going by its own records."""
        if self.epoch is None:
            raise LeaseLost(f"{self.holder} doesn't hold the lease")
        if self.clock() >= self.expires_at - self.MARGIN:
            raise LeaseLost(f"{self.holder}'s lease has expired (was the process paused or offline?)")

    def verify(self) -> None:
        """check(), then confirm against the stored lease. Default: check() only."""
        self.check()

    @abstractmethod
    def acquire(self, ttl: float) -> bool:
        """Take the lease for `ttl` seconds if it's free, expired or already ours."""

    @abstractmethod
    def renew(self, ttl: float) -> bool:
        """Extend our lease to `ttl` seconds from now. False (and no longer held) if someone took over."""

    @abstractmethod
    def release(self) -> None:
        """Give the lease up early so the other side can take over straight away."""

    @abstractmethod
    def read(self) -> LeaseInfo | None:
        """The stored lease, whoever holds it."""

    def request_handoff(self) -> None:
        """Ask the current holder to finish up and release. Default: no-op."""

    def handoff_requested(self) -> str | None:
        """Who asked us to hand over, if anyone did recently. Default: nobody."""
        return None


class LocalLease(Lease):
    """For a single process with local state (JSON storage, tests): always granted."""

    def acquire(self, ttl: float) -> bool:
        self.epoch, self.expires_at = 1, math.inf
        return True

    def renew(self, ttl: float) -> bool:
        return self.epoch is not None

    def release(self) -> None:
        self.epoch = None

    def read(self) -> LeaseInfo | None:
        if self.epoch is None:
            return None
        return LeaseInfo(holder=self.holder, epoch=self.epoch, expires_at=self.expires_at)


class LeaseKeeper:
    """Keeps a lease renewed from a background thread, for long synchronous jobs (migrate).

        with LeaseKeeper(lease):
            ...   # lease.check() keeps passing while renewals succeed
    """

    def __init__(self, lease: Lease, ttl: float = 180.0, every: float = 60.0) -> None:
        self.lease, self.ttl, self.every = lease, ttl, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)

    def __enter__(self) -> Lease:
        self._thread.start()
        return self.lease

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        wait = self.every
        while not self._stop.wait(wait):
            try:
                if not self.lease.renew(self.ttl):
                    return  # taken over: the job's next lease check stops it
                wait = self.every
            except Exception:  # e.g. offline; try again soon
                wait = min(self.every, 10.0)
