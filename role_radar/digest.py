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

    # Read only the configured companies; no table scan or additional IAM permission.
    limit = asyncio.Semaphore(8)

    async def load(name: str) -> CompanyRecord:
        async with limit:
            return await asyncio.to_thread(store.load_company, name)

    records = await asyncio.gather(*(load(name) for name in companies))
    if deadline is not None and time.monotonic() + 120 >= deadline:
        return result  # leave this slot due for the next runner
    dirty: set[str] = set()
    groups: list[tuple[CompanyRecord, JobPosting, list[tuple[str, SeenJob]]]] = []
    for record in records:
        pending: dict[str, list[tuple[str, SeenJob]]] = {}
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
                pending.setdefault(job.fingerprint, []).append((uid, job))
        for aliases in pending.values():
            uid, job = aliases[0]
            posting = JobPosting(company=record.name, title=job.title, location=job.location,
                                 url=job.url, source="digest")
            posting._uid = uid
            # A repost/duplicate that shares a fingerprint also shares receipts.
            delivered = {channel: stamp for _, alias in aliases for channel, stamp in alias.notified_channels.items()}
            for _, alias in aliases:
                if delivered != alias.notified_channels:
                    alias.notified_channels.update(delivered)
                    dirty.add(record.name)
            groups.append((record, posting, aliases))

    # Claim the interval before sending. An interrupted invocation must not send
    # a second digest in this interval; pending jobs are retried next interval.
    lease.check()
    await asyncio.to_thread(lease.verify)
    schedule.next_send_at = to_iso(next_boundary(now, minutes))
    schedule.last_attempt_at = to_iso(now)
    await asyncio.to_thread(store.save_digest, schedule)
    result.next_due = from_iso(schedule.next_send_at)
    result.jobs = len(groups)

    async def persist() -> None:
        for record in records:
            if record.name in dirty:
                record.delivery_changed = True
                await save(record)
                dirty.remove(record.name)

    if not groups:
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
        log.error("No digest channel configured; %d job(s) remain pending", len(groups))

    for channel in channels:
        pending = [group for group in groups if channel.name not in group[2][0][1].notified_channels]
        if not pending:
            continue
        await asyncio.to_thread(lease.verify)
        if await notify_all([channel], [group[1] for group in pending], check=lease.check):
            for record, _, aliases in pending:
                for _, alias in aliases:
                    alias.notified_channels[channel.name] = to_iso(now)
                dirty.add(record.name)
            # Save a channel's success before attempting another channel.
            await persist()
        else:
            result.failed = True

    for record, _, aliases in groups:
        uid, job = aliases[0]
        if channels and all(channel.name in job.notified_channels for channel in channels):
            job.notified_at = to_iso(now)
            record.alerted.append(uid)
            for _, duplicate in aliases[1:]:
                duplicate.duplicate_of = uid
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
