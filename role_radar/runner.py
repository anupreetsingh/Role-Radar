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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from role_radar.backends import Backend, ConfigSource
from role_radar.config import AppConfig
from role_radar.http_client import RobotsCache
from role_radar.lease import Lease
from role_radar.monitor import Notifiers, PassResult, Unsaved, run_pass
from role_radar.storage import switch_on

log = logging.getLogger("runner")

LAPTOP_TTL = 180.0  # lease length for the laptop and `run --once`...
RENEW_EVERY = 60.0  # ...renewed this often
RENEW_RETRY = 10.0  # after a failed renewal, try again this soon
WAIT_POLL = 15.0  # how often a runner without the lease tries again
MIN_SLEEP, MAX_SLEEP = 15.0, 60.0  # between passes while holding the lease
CONFIG_REFRESH = 300.0  # re-read the config at most this often (S3 only resends it if changed)
LAMBDA_WORK_SECONDS = 600.0  # Lambda stops starting companies after this long
LAMBDA_LEASE_MARGIN = 60.0  # Lambda's lease outlives the invocation's time limit by this much
HANDOFF_POLL = 10.0  # how often Lambda looks for a handoff request
SWITCH_POLL = 30.0  # how often a pass in progress re-reads its runner's on/off switch
EXIT_LEASE_HELD = 4
UNREACHABLE = "(unreachable)"  # _take_lease() couldn't reach the store at all


@dataclass
class Skipped:
    """A Lambda run that did nothing, and why."""

    reason: str


class Runner:
    def __init__(
        self,
        source: ConfigSource,
        backend: Backend,
        notifiers: Notifiers,
        *,
        clock: Callable[[], float] = time.time,
        robots: RobotsCache | None = None,
        name: str | None = None,
    ) -> None:
        self.source = source
        self.store = backend.store
        self.lease = backend.lease
        self.name = name or backend.lease.holder  # its passes are recorded under this, for `status`
        self.notifiers = notifiers
        self.clock = clock
        self.robots = robots or RobotsCache()
        self._config: AppConfig | None = None
        self._config_read_at = 0.0
        self.unsaved = Unsaved()  # alerts sent whose save failed, so they aren't sent again
        # Lease calls get a thread of their own: saves queued in the shared
        # pool mustn't delay a renewal, and a release runs after any renewal in flight.
        self._lease_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lease")

    async def _lease_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._lease_thread, fn, *args)

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

    def now(self) -> datetime:
        """The runner's clock as a UTC datetime: lease expiry and the schedule use the same time."""
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).replace(microsecond=0)

    async def pass_once(self, record_idle: bool = False, **kwargs: Any) -> PassResult:
        """One pass with the current config. The caller holds the lease (unless it's a dry run).

        The pass is recorded for `status` if it checked anything (or `record_idle`).
        """
        config = await asyncio.to_thread(self.config)
        result = await run_pass(
            config, self.store, self.notifiers,
            lease=self.lease, robots=self.robots, unsaved=self.unsaved, clock=self.now, **kwargs,
        )  # fmt: skip
        if (result.checked or result.lease_lost or result.digest_attempted or result.digest_failed or record_idle) and not kwargs.get("dry_run"):
            try:
                await asyncio.to_thread(self.store.record_run, self.name, {**result.summary(), "holder": self.lease.holder})
            except Exception as exc:  # informational only
                log.warning("Couldn't record the pass: %s", exc)
        return result

    # -- laptop ------------------------------------------------------------

    async def serve(self, stop: asyncio.Event) -> int:
        """`role-radar start`: check companies whenever this runner holds the lease, until `stop` is set.

        While the laptop's switch is off (`role-radar switch laptop off`), it gives
        up the lease, so Lambda covers if its own switch is on, and idles until
        switched back on.
        """
        renewer: asyncio.Task[None] | None = None
        was_off = False
        try:
            while not stop.is_set():
                if not await self.switched_on():
                    if not was_off:
                        log.info("The laptop runner is switched off; idling (role-radar switch laptop on)")
                        was_off = True
                    if renewer:
                        renewer.cancel()
                        renewer = None
                    if self.lease.held:
                        await self._lease_call(self._release)
                    await _wait(stop, MAX_SLEEP)
                    continue
                if was_off:
                    log.info("The laptop runner is switched on again")
                    was_off = False
                if not self.lease.held:
                    if renewer:
                        renewer.cancel()
                    if not await self._wait_for_lease(stop):
                        continue  # stopped, or switched off while waiting
                    renewer = asyncio.create_task(self._keep_renewing())
                await _wait(stop, await self._serve_pass(stop))
        finally:
            if renewer:
                renewer.cancel()
            await self._lease_call(self._release)
        return 0

    async def _wait_for_lease(self, stop: asyncio.Event) -> bool:
        """Take the lease, asking whoever has it to hand over and retrying. False if stopped first."""
        waiting_for: str | None = None
        while not stop.is_set() and await self.switched_on():
            holder = await self._take_lease()
            if not holder:
                return True
            if holder != waiting_for:
                if holder == UNREACHABLE:
                    log.warning("Can't reach the lease (offline?); retrying every %.0fs", WAIT_POLL)
                else:
                    log.info("%s has the lease; asked it to hand over, will take over when it does", holder)
                waiting_for = holder
            await _wait(stop, WAIT_POLL)
        return False

    async def _serve_pass(self, stop: asyncio.Event) -> float:
        """One pass while holding the lease. Returns how long to wait before the next."""
        switch = _SwitchWatch(self.switched_on)

        async def should_stop() -> bool:
            return stop.is_set() or not await switch.on()

        try:
            result = await self.pass_once(should_stop=should_stop)
        except Exception as exc:  # e.g. DynamoDB or S3 unreachable; try again shortly
            log.error("Pass failed: %s", exc)
            return WAIT_POLL
        if result.lease_lost:
            log.warning("Lost the lease; will take it back once it's free")
            return 0
        return self._pause_after(result)

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
            await self._lease_call(self._release)

    # -- Lambda -------------------------------------------------------------

    async def lambda_pass(
        self, remaining: float, work_seconds: float = LAMBDA_WORK_SECONDS, holder: str | None = None
    ) -> PassResult | Skipped:
        """Lambda: take the lease for the rest of this invocation and check what's due.

        Skipped at once if another runner (normally the laptop) holds the
        lease, or if no config has been pushed yet. Otherwise stops starting
        companies after `work_seconds`, or as soon as the laptop asks to take
        over, lets the ones in flight finish (each is cut short to fit in
        `remaining`), and releases the lease. `holder` names this invocation,
        so an overlapping one waits rather than taking the lease over.
        """
        if holder:
            self.lease.holder = holder
        started = time.monotonic()
        try:
            await asyncio.to_thread(self.config)
        except FileNotFoundError as exc:  # deployed, but `role-radar config push` hasn't run yet
            return Skipped(str(exc))
        if not await self.switched_on("lambda"):
            return Skipped("Lambda is switched off (role-radar switch lambda on)")
        if not await self._lease_call(self.lease.acquire, remaining + LAMBDA_LEASE_MARGIN):
            info = await self._lease_call(self.lease.read)
            return Skipped(f"{info.holder if info and info.holder else 'another runner'} holds the lease")
        stop_starting_at = started + min(work_seconds, remaining)
        handoff = _HandoffWatch(self.lease, self._lease_call)

        async def should_stop() -> bool:
            return time.monotonic() >= stop_starting_at or await handoff.requested()

        try:
            # Recorded even when nothing was due, so `status` shows Lambda is alive.
            return await self.pass_once(should_stop=should_stop, deadline=started + remaining, record_idle=True)
        finally:
            await self._lease_call(self._release)

    async def switched_on(self, runner: str = "laptop") -> bool:
        """Whether `runner`'s on/off switch is on. On if the store can't be read, as before switches existed."""
        try:
            return switch_on(await asyncio.to_thread(self.store.load_switches), runner)
        except Exception as exc:
            log.warning("Couldn't read the runner switches (%s); assuming %s is on", exc, runner)
            return True

    async def _take_lease(self, ask: bool = True) -> str | None:
        """Try to take the lease. Returns None on success, otherwise who has it (after asking them to hand over)."""
        try:
            if await self._lease_call(self.lease.acquire, LAPTOP_TTL):
                log.info("Took the lease (epoch %s); checking companies as they come due", self.lease.epoch)
                return None
            info = await self._lease_call(self.lease.read)
            if ask:
                await self._lease_call(self.lease.request_handoff)
            return info.holder if info and info.holder else "another runner"
        except Exception as exc:  # offline, credentials missing...
            log.warning("Couldn't reach the lease: %s", exc)
            return UNREACHABLE

    async def _keep_renewing(self) -> None:
        wait = RENEW_EVERY
        while True:
            await asyncio.sleep(wait)
            try:
                if not await self._lease_call(self.lease.renew, LAPTOP_TTL):
                    return  # someone took over; the next save or check notices
                wait = RENEW_EVERY
            except Exception as exc:  # offline, throttled...: the lease lasts 3 minutes, so retry soon
                log.warning("Couldn't renew the lease (%s); retrying in %.0fs", exc, RENEW_RETRY)
                wait = RENEW_RETRY

    def _release(self) -> None:
        try:
            self.lease.release()
        except Exception as exc:  # it expires on its own within LAPTOP_TTL
            log.warning("Couldn't release the lease (%s); it expires on its own", exc)

    def _pause_after(self, result: PassResult) -> float:
        if result.next_due is None:
            return MAX_SLEEP
        return min(max((result.next_due - self.now()).total_seconds(), MIN_SLEEP), MAX_SLEEP)


class _SwitchWatch:
    """A pass's view of its runner's switch, re-read at most every SWITCH_POLL seconds."""

    def __init__(self, read: Callable[[], Awaitable[bool]]) -> None:
        self.read = read
        self.checked_at = time.monotonic()  # the serve loop just read it
        self.value = True

    async def on(self) -> bool:
        if self.value and time.monotonic() - self.checked_at >= SWITCH_POLL:
            self.checked_at = time.monotonic()
            self.value = await self.read()
            if not self.value:
                log.info("Switched off mid-pass; finishing the companies in flight, then stopping")
        return self.value


class _HandoffWatch:
    """Lambda's view of whether the laptop has asked to take over, re-read at most every HANDOFF_POLL seconds."""

    def __init__(self, lease: Lease, call: Callable[..., Awaitable[Any]]) -> None:
        self.lease = lease
        self._call = call  # runs the (blocking) read off the event loop
        self._checked_at = -HANDOFF_POLL
        self._asked_by: str | None = None

    async def requested(self) -> bool:
        now = time.monotonic()
        if self._asked_by is None and now - self._checked_at >= HANDOFF_POLL:
            self._checked_at = now
            try:
                self._asked_by = await self._call(self.lease.handoff_requested)
            except Exception as exc:  # can't tell; carry on until the next check
                log.warning("Couldn't check for a handoff request: %s", exc)
            if self._asked_by:
                log.info("%s asked to take over; finishing the companies in progress", self._asked_by)
        return self._asked_by is not None


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, or until `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
