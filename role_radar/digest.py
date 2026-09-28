"""Send one shared digest per interval, using unnotified matching jobs as the queue.

The cadence and delivery receipts live in the state store, so a cold Lambda or
laptop handoff cannot reset the interval. Empty intervals send nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from role_radar.lease import Lease
from role_radar.models import JobPosting
from role_radar.notifications import Notifier, notify_all
from role_radar.storage import CompanyRecord, DigestSchedule, SeenJob, StateStore, from_iso, to_iso

log = logging.getLogger(__name__)


@dataclass
class DigestResult:
    next_due: datetime | None = None
    attempted: bool = False
    jobs: int = 0
    completed: int = 0
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
) -> DigestResult:
    schedule = await asyncio.to_thread(store.load_digest)
    if not schedule.next_send_at or schedule.interval_minutes != minutes:
        schedule = DigestSchedule(next_send_at=to_iso(next_boundary(now, minutes)), interval_minutes=minutes)
        lease.check()
        await asyncio.to_thread(store.save_digest, schedule)
    result = DigestResult(next_due=from_iso(schedule.next_send_at))
    if now < result.next_due:
        return result

    # Read only the configured companies with matches still to send (or receipts from a
    # failed save to apply): reading every company's jobs takes minutes of a small
    # table's read capacity. A company whose schedule row predates the pending count
    # is read once its next check has counted them.
    metas = await asyncio.to_thread(store.load_schedule)
    carried = set(sent or {}) | set(receipts or {})
    names = [name for name in companies if name in carried or (name in metas and metas[name].pending)]
    limit = asyncio.Semaphore(8)

    async def load(name: str) -> CompanyRecord:
        async with limit:
            return await asyncio.to_thread(store.load_company, name)

    records = await asyncio.gather(*(load(name) for name in names))
    if deadline is not None and time.monotonic() + 120 >= deadline:
        return result  # leave this slot due for the next runner
    dirty: set[str] = set()
    queued: list[tuple[CompanyRecord, JobPosting, SeenJob]] = []
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
            if job.matched and not job.notified_at and not job.duplicate_of:
                posting = JobPosting(company=record.name, title=job.title, location=job.location,
                                     url=job.url, source="digest")
                posting._uid = uid
                queued.append((record, posting, job))

    # Claim the interval before sending. An interrupted invocation must not send
    # a second digest in this interval; pending jobs are retried next interval.
    lease.check()
    await asyncio.to_thread(lease.verify)
    schedule.next_send_at = to_iso(next_boundary(now, minutes))
    schedule.last_attempt_at = to_iso(now)
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
        return result
    result.attempted = True
    try:
        channels = list(await asyncio.to_thread(notifiers))
    except Exception as exc:
        log.error("Couldn't load digest channels (%s); jobs remain pending", type(exc).__name__)
        channels = []
    if not channels:
        result.failed = True
        log.error("No digest channel configured; %d job(s) remain pending", len(queued))

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
    log.info("Digest: %d job(s), %d completed, pending delivery=%s, next at %s",
             result.jobs, result.completed, result.failed, schedule.next_send_at)
    return result
