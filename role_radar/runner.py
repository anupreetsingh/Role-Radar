"""Holding the lease and running passes: `role-radar start`, `run --once`, and Lambda.

  laptop (`role-radar start`)              Lambda (every 5 minutes)
  ─────────────────────────────            ────────────────────────
  take the lease for 3 minutes and renew   take the lease for the whole invocation,
  it every minute. If someone else holds   or exit at once if someone else holds it
  it, ask them to hand over, retry every
  15 s
  check whatever is due, then sleep until  check whatever is due until 10 minutes
  the next company is due (at most 60 s)   are up or the laptop asks to take over
  on Ctrl+C / SIGTERM / SIGHUP: finish     release the lease
  the companies in flight, release

The schedule is shared, so each side simply continues where the other stopped.
Quitting the laptop app releases the lease, and Lambda takes over at its next
run. Closing the lid or losing the network stops the renewals, so the lease
expires within 3 minutes and Lambda takes over at its next run after that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from role_radar.backends import Backend, ConfigSource
from role_radar.config import AppConfig
from role_radar.http_client import RobotsCache
from role_radar.monitor import Notifiers, PassResult, run_pass
from role_radar.storage import utcnow

log = logging.getLogger("runner")

LAPTOP_TTL = 180.0  # lease length for the laptop and `run --once`...
RENEW_EVERY = 60.0  # ...renewed this often
WAIT_POLL = 15.0  # how often a runner without the lease tries again
MIN_SLEEP, MAX_SLEEP = 15.0, 60.0  # between passes while holding the lease
CONFIG_REFRESH = 300.0  # re-read the config at most this often (S3 only resends it if changed)
EXIT_LEASE_HELD = 4
UNREACHABLE = "(unreachable)"  # _take_lease() couldn't reach the store at all


class Runner:
    def __init__(
        self,
        source: ConfigSource,
        backend: Backend,
        notifiers: Notifiers,
        *,
        clock: Callable[[], float] = time.time,
        robots: RobotsCache | None = None,
    ) -> None:
        self.source = source
        self.store = backend.store
        self.lease = backend.lease
        self.notifiers = notifiers
        self.clock = clock
        self.robots = robots or RobotsCache()
        self._config: AppConfig | None = None
        self._config_read_at = 0.0

    def config(self) -> AppConfig:
        """The companies config, re-read at most every CONFIG_REFRESH seconds."""
        now = self.clock()
        if self._config is None or now - self._config_read_at >= CONFIG_REFRESH:
            try:
                self._config = self.source.load()
            except Exception as exc:
                if self._config is None:
                    raise
                log.warning("Couldn't re-read the config from %s (%s); keeping the previous one", self.source.description, exc)
            self._config_read_at = now
        return self._config

    async def pass_once(self, **kwargs: Any) -> PassResult:
        """One pass with the current config. The caller holds the lease (unless it's a dry run)."""
        config = await asyncio.to_thread(self.config)
        result = await run_pass(config, self.store, self.notifiers, lease=self.lease, robots=self.robots, **kwargs)
        if (result.checked or result.lease_lost) and not kwargs.get("dry_run"):
            try:
                await asyncio.to_thread(self.store.record_run, self.lease.holder, result.summary())
            except Exception as exc:  # informational only
                log.warning("Couldn't record the pass: %s", exc)
        return result

    # -- laptop ------------------------------------------------------------

    async def serve(self, stop: asyncio.Event) -> int:
        """`role-radar start`: check companies whenever this runner holds the lease, until `stop` is set."""
        renewer: asyncio.Task[None] | None = None
        waiting_for: str | None = None
        try:
            while not stop.is_set():
                if not self.lease.held:
                    if renewer:
                        renewer.cancel()
                        renewer = None
                    holder = await self._take_lease()
                    if holder:
                        if holder != waiting_for:
                            if holder == UNREACHABLE:
                                log.warning("Can't reach the lease (offline?); retrying every %.0fs", WAIT_POLL)
                            else:
                                log.info("%s has the lease; asked it to hand over, will take over when it does", holder)
                            waiting_for = holder
                        await _wait(stop, WAIT_POLL)
                        continue
                    waiting_for = None
                    renewer = asyncio.create_task(self._keep_renewing())
                try:
                    result = await self.pass_once(should_stop=stop.is_set)
                except Exception as exc:  # e.g. DynamoDB or S3 unreachable; try again shortly
                    log.error("Pass failed: %s", exc)
                    await _wait(stop, WAIT_POLL)
                    continue
                if result.lease_lost:
                    log.warning("Lost the lease; will take it back once it's free")
                    continue
                await _wait(stop, self._pause_after(result))
        finally:
            if renewer:
                renewer.cancel()
            await asyncio.to_thread(self._release)
        return 0

    async def run_once(self, stop: asyncio.Event | None = None, **kwargs: Any) -> int:
        """`role-radar run --once`: take the lease, run one pass, release it."""
        holder = await self._take_lease(ask=False)
        if holder == UNREACHABLE:
            return EXIT_LEASE_HELD
        if holder:
            log.error("%s has the lease, so a run now would clash with it. Stop it first, or try again later.", holder)
            return EXIT_LEASE_HELD
        renewer = asyncio.create_task(self._keep_renewing())
        try:
            result = await self.pass_once(should_stop=stop.is_set if stop else None, **kwargs)
            return result.exit_code
        finally:
            renewer.cancel()
            await asyncio.to_thread(self._release)

    async def _take_lease(self, ask: bool = True) -> str | None:
        """Try to take the lease. Returns None on success, otherwise who has it (after asking them to hand over)."""
        try:
            if await asyncio.to_thread(self.lease.acquire, LAPTOP_TTL):
                log.info("Took the lease (epoch %s); checking companies as they come due", self.lease.epoch)
                return None
            info = await asyncio.to_thread(self.lease.read)
            if ask:
                await asyncio.to_thread(self.lease.request_handoff)
            return info.holder if info and info.holder else "another runner"
        except Exception as exc:  # offline, credentials missing...
            log.warning("Couldn't reach the lease: %s", exc)
            return UNREACHABLE

    async def _keep_renewing(self) -> None:
        while True:
            await asyncio.sleep(RENEW_EVERY)
            try:
                if not await asyncio.to_thread(self.lease.renew, LAPTOP_TTL):
                    return  # someone took over; the next save or check notices
            except Exception as exc:
                log.warning("Couldn't renew the lease (%s); will retry", exc)

    def _release(self) -> None:
        try:
            self.lease.release()
        except Exception as exc:  # it expires on its own within LAPTOP_TTL
            log.warning("Couldn't release the lease (%s); it expires on its own", exc)

    @staticmethod
    def _pause_after(result: PassResult) -> float:
        if result.next_due is None:
            return MAX_SLEEP
        return min(max((result.next_due - utcnow()).total_seconds(), MIN_SLEEP), MAX_SLEEP)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, or until `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
