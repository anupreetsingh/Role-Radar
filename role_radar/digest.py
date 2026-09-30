"""Send one shared digest per interval, using unnotified matching jobs as the queue.

The cadence and delivery receipts live in the state store, so a cold Lambda or
laptop handoff cannot reset the interval. Empty intervals send nothing. While
every alert channel is switched off, matches collect in Live Tracking instead,
and go out in the first digest after one is switched back on.

Live Tracking can change what the next digest does, alerts on or off: matches
sent from there go at once (to the channels switched on, or every one set up
when all are off), and a match skipped there is recorded as notified, with the
reason, instead of being sent. Until that digest the skip can be undone.

A pass in progress also sends the digest when it's due (monitor.run_pass), so
a company being checked right then is left for the next digest: `locks` holds
each company's lock, taken by its check and by the digest while it has the
company's state loaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, MutableMapping, Sequence

from role_radar.lease import Lease
from role_radar.models import JobPosting
from role_radar.notifications import Notifier, notify_all, switched_on
from role_radar.storage import (
    SKIPPED, CompanyRecord, DigestSchedule, QueuedMatch, SeenJob, StateStore, alerts_off, from_iso, to_iso, waiting,
)

log = logging.getLogger(__name__)


@dataclass
class DigestResult:
    next_due: datetime | None = None
    attempted: bool = False
    jobs: int = 0
    completed: int = 0
    skipped: int = 0  # matches skipped in Live Tracking, recorded without sending
    failed: bool = False


def next_boundary(now: datetime, minutes: float) -> datetime:
    seconds = minutes * 60
    return datetime.fromtimestamp((now.timestamp() // seconds + 1) * seconds, timezone.utc)


async def flush_digest(
    store: StateStore,
    companies: list[str],
    notifiers: Callable[[], Sequence[Notifier]],
    lease: Lease,
    now: datetime,
    minutes: float,
    save: Callable[[CompanyRecord], Awaitable[None]],
    *,
    deadline: float | None = None,
    sent: dict[str, dict[str, str]] | None = None,
    receipts: dict[str, dict[str, dict[str, str]]] | None = None,
    locks: MutableMapping[str, asyncio.Lock] | None = None,
    save_lock: asyncio.Lock | None = None,
) -> DigestResult:
    guard = save_lock or contextlib.nullcontext()
    schedule = await asyncio.to_thread(store.load_digest)
    if not schedule.next_send_at or schedule.interval_minutes != minutes:
        schedule = DigestSchedule(next_send_at=to_iso(next_boundary(now, minutes)), interval_minutes=minutes,
                                  last_attempt_at=schedule.last_attempt_at, requested_at=schedule.requested_at)
        lease.check()
        async with guard:
            await asyncio.to_thread(store.save_digest, schedule)
    result = DigestResult(next_due=from_iso(schedule.next_send_at))
    if now < result.next_due and not schedule.requested:
        return result
    switches = await asyncio.to_thread(store.load_switches)
    # At the digest time with alerts on, every waiting match goes. Otherwise only those sent from
    # Live Tracking do, and the skips made there are recorded: the rest collect there.
    send_all = not alerts_off(switches) and now >= result.next_due
    if not send_all:
        log.debug("Alerts are switched off or it isn't the digest time: sending only what Live Tracking sent")

    # Read only the configured companies with matches still to send (or receipts from a
    # failed save to apply, or matches skipped or sent in Live Tracking): reading every
    # company's jobs takes minutes of a small table's read capacity. A company whose
    # schedule row predates the pending count is read once its next check has counted them.
    marked = {m.company for m in await asyncio.to_thread(store.load_queue) if not m.done_at and (m.skipped_at or m.send_at)}
    metas = await asyncio.to_thread(store.load_schedule) if send_all else {}
    carried = set(sent or {}) | set(receipts or {})
    names = [name for name in companies if name in carried or name in marked or (name in metas and metas[name].pending)]
    held: list[asyncio.Lock] = []
    busy: set[str] = set()
    if locks is not None:
        for name in names:
            lock = locks.setdefault(name, asyncio.Lock())
            if lock.locked():  # being checked right now: its matches go in the next digest
                busy.add(name)
            else:
                await lock.acquire()  # free, so this doesn't wait
                held.append(lock)
    try:
        return await _send(store, [n for n in names if n not in busy], busy, notifiers, lease, now, minutes, save,
                           schedule, switches, result, send_all=send_all, deadline=deadline, sent=sent, receipts=receipts,
                           guard=guard)
    finally:
        for lock in held:
            lock.release()


async def _send(
    store: StateStore,
    names: list[str],
    busy: set[str],
    notifiers: Callable[[], Sequence[Notifier]],
    lease: Lease,
    now: datetime,
    minutes: float,
    save: Callable[[CompanyRecord], Awaitable[None]],
    schedule: DigestSchedule,
    switches: dict[str, bool],
    result: DigestResult,
    *,
    send_all: bool,
    deadline: float | None,
    sent: dict[str, dict[str, str]] | None,
    receipts: dict[str, dict[str, dict[str, str]]] | None,
    guard: contextlib.AbstractAsyncContextManager,
) -> DigestResult:
    limit = asyncio.Semaphore(8)

    async def load(name: str) -> CompanyRecord:
        async with limit:
            return await asyncio.to_thread(store.load_company, name)

    records = await asyncio.gather(*(load(name) for name in names))
    if deadline is not None and time.monotonic() + 120 >= deadline:
        return result  # leave this slot due for the next runner
    listed = {(m.company, m.uid): m for m in await asyncio.to_thread(store.load_queue)}
    dirty: set[str] = set()
    waiting_now: list[tuple[CompanyRecord, JobPosting, SeenJob]] = []
    for record in records:
        for uid, job in sorted(record.jobs.items(), key=lambda item: (item[1].first_seen, item[0])):
            remembered = (receipts or {}).get(record.name, {}).get(uid, {})
            if remembered:
                job.notified_channels.update(remembered)
                dirty.add(record.name)
            stamp = (sent or {}).get(record.name, {}).get(uid)
            if stamp and not job.notified_at:
                job.notified_at = stamp
                record.alerted.append(uid)
                dirty.add(record.name)
            if not waiting(job):
                continue
            mark = listed.get((record.name, uid))
            if mark and mark.skipped_at:
                job.notified_at, job.dropped_for = to_iso(now), SKIPPED
                dirty.add(record.name)
                result.skipped += 1
                continue
            posting = JobPosting(company=record.name, title=job.title, location=job.location,
                                 url=job.url, source="digest")
            posting._uid = uid
            waiting_now.append((record, posting, job))
    await _repair_list(store, listed, records, waiting_now, busy, now, everyone=send_all)
    queued = waiting_now if send_all else [
        item for item in waiting_now if (mark := listed.get((item[0].name, item[1].uid))) and mark.send_at
    ]
    queued.sort(key=lambda item: (item[0].name.lower(), item[1].uid))
    queued.sort(key=lambda item: item[2].first_seen, reverse=True)  # newest first; the same moment by company

    # Claim the interval before sending. An interrupted invocation must not send
    # a second digest in this interval; pending jobs are retried next interval.
    lease.check()
    await asyncio.to_thread(lease.verify)
    schedule.next_send_at = to_iso(next_boundary(now, minutes))
    schedule.last_attempt_at = to_iso(now)
    async with guard:
        await asyncio.to_thread(store.save_digest, schedule)
    result.next_due = from_iso(schedule.next_send_at)
    result.jobs = len(queued)

    async def persist() -> None:
        for record in records:
            if record.name in dirty:
                record.delivery_changed = True
                await save(record)
                dirty.remove(record.name)

    if not queued:
        await persist()
        if result.skipped:
            log.info("Digest: nothing to send; %d skipped match(es) recorded", result.skipped)
        return result
    result.attempted = True
    try:
        # Matches sent from Live Tracking with every channel switched off go to every one set up.
        channels = switched_on(await asyncio.to_thread(notifiers), {} if alerts_off(switches) else switches)
    except Exception as exc:
        log.error("Couldn't load digest channels (%s); jobs remain pending", type(exc).__name__)
        channels = []
    if not channels:
        result.failed = True
        log.error("No digest channel is both configured and switched on; %d job(s) remain pending", len(queued))

    for channel in channels:
        pending = [item for item in queued if channel.name not in item[2].notified_channels]
        if not pending:
            continue
        await asyncio.to_thread(lease.verify)
        if await notify_all([channel], [posting for _, posting, _ in pending], check=lease.check):
            for record, _, job in pending:
                job.notified_channels[channel.name] = to_iso(now)
                dirty.add(record.name)
            # Save a channel's success before attempting another channel.
            await persist()
        else:
            result.failed = True

    for record, posting, job in queued:
        if channels and all(channel.name in job.notified_channels for channel in channels):
            job.notified_at = to_iso(now)
            record.alerted.append(posting.uid)
            dirty.add(record.name)
            result.completed += 1
    await persist()
    if result.failed:
        invalidate = getattr(notifiers, "invalidate", None)
        if invalidate:
            invalidate()
    log.info("Digest: %d job(s), %d completed, %d skipped, pending delivery=%s, next at %s",
             result.jobs, result.completed, result.skipped, result.failed, schedule.next_send_at)
    return result


async def _repair_list(
    store: StateStore,
    listed: dict[tuple[str, str], QueuedMatch],
    records: Sequence[CompanyRecord],
    queued: list[tuple[CompanyRecord, JobPosting, SeenJob]],
    busy: set[str],
    now: datetime,
    *,
    everyone: bool = True,
) -> None:
    """Make Live Tracking's list match what's actually waiting.

    Adds the matches it lacks (those from before the list existed) and drops rows
    whose match is no longer waiting. A row that joined after `now` may belong to
    a check that finished meanwhile, so it stays, as do those of companies still
    being checked. `everyone`: `records` has every company with a match waiting, so
    rows of any other company are left over; otherwise only these companies' rows are looked at.
    """
    loaded = {record.name for record in records}
    wanted = {(record.name, posting.uid) for record, posting, _ in queued}
    skipping = {(r.name, uid) for r in records for uid, job in r.jobs.items() if job.dropped_for == SKIPPED}
    add = [QueuedMatch(record.name, posting.uid, job.title, job.url, job.first_seen, job.location)
           for record, posting, job in queued if (record.name, posting.uid) not in listed]
    remove = [key for key, m in listed.items()
              if key not in wanted and key not in skipping and not m.done_at and m.company not in busy
              and (everyone or m.company in loaded) and (m.queued_at or "") < to_iso(now)]
    if not add and not remove:
        return
    try:
        await asyncio.to_thread(store.repair_queue, add, remove)
        log.info("Live Tracking list: added %d waiting match(es), removed %d stale", len(add), len(remove))
    except Exception as exc:  # the list is a view; the digest goes on
        log.warning("Couldn't update the Live Tracking list (%s)", exc)
