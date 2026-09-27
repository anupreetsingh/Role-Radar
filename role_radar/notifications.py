"""Notification channels.

All new jobs from one run go out as a single batch per channel. Jobs with the
same company + title (e.g. one role in several cities) are grouped into one
entry so they don't read as duplicate alerts.

Channels are configured from these settings (environment variables, or the
same names in SSM Parameter Store), so secrets never live in the repo:
  DISCORD_WEBHOOK_URL
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SECURITY, EMAIL_FROM, EMAIL_TO
With environment variables and none set, alerts are printed to stdout (handy
locally). With SSM, no channels means alerts stay pending, so they're never
lost to a log nobody reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

import httpx

from role_radar.models import JobPosting, normalize_text
from role_radar.storage import SeenJob, to_iso, utcnow

log = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    """A delivery failure whose message contains no credentials or recipient details."""


# -- formatting ---------------------------------------------------------------


@dataclass
class JobGroup:
    company: str
    title: str
    jobs: list[JobPosting]

    @property
    def locations(self) -> list[str]:
        return list(dict.fromkeys(j.location or "Not specified" for j in self.jobs))

    @property
    def posted(self) -> str | None:
        dates = [j.date_posted for j in self.jobs if j.date_posted]
        if not dates:
            return None
        d = min(dates)
        return f"{d:%B} {d.day}, {d.year}"  # "September 25, 2026" (portable, no %-d)


def group_jobs(jobs: list[JobPosting]) -> list[JobGroup]:
    groups: dict[tuple[str, str], JobGroup] = {}
    for job in sorted(jobs, key=lambda j: (j.company.lower(), j.title.lower(), j.location or "")):
        key = (normalize_text(job.company), normalize_text(job.title))
        groups.setdefault(key, JobGroup(job.company, job.title, [])).jobs.append(job)
    return list(groups.values())


def format_group(group: JobGroup) -> str:
    lines = ["NEW JOB", "", f"Company: {group.company}", f"Title: {group.title}"]
    locations = group.locations
    lines.append(f"Location{'s' if len(locations) > 1 else ''}: {' | '.join(locations)}")
    first = group.jobs[0]
    if first.employment_type:
        lines.append(f"Type: {first.employment_type}")
    if group.posted:
        lines.append(f"Posted: {group.posted}")
    lines += ["", "Apply:"]
    urls = list(dict.fromkeys(j.url for j in group.jobs))
    if len(urls) == 1:
        lines.append(urls[0])
    else:
        lines += [f"{j.location or 'Link'}: {j.url}" for j in group.jobs]
    return "\n".join(lines)


def headline(jobs: list[JobPosting]) -> str:
    companies = sorted({j.company for j in jobs})
    shown = ", ".join(companies[:3]) + (f" +{len(companies) - 3} more" if len(companies) > 3 else "")
    return f"{len(jobs)} new matching job{'s' if len(jobs) != 1 else ''} — {shown}"


def format_digest(jobs: list[JobPosting]) -> str:
    lines = ["Role Radar — New jobs digest", headline(jobs)]
    company = None
    for group in group_jobs(jobs):
        if group.company != company:
            company = group.company
            lines += ["", company]
        lines.append(f"- {group.title} — {' | '.join(group.locations)}")
        lines.extend(f"  {url}" for url in dict.fromkeys(job.url for job in group.jobs))
    return "\n".join(lines)


# -- channels -------------------------------------------------------------------


class Notifier(ABC):
    name: str = "notifier"

    @abstractmethod
    async def send(self, jobs: list[JobPosting]) -> None:
        """Deliver all jobs. Raise on failure so the jobs are retried next run."""


class ConsoleNotifier(Notifier):
    name = "console"

    async def send(self, jobs: list[JobPosting]) -> None:
        sep = "\n" + "-" * 60 + "\n"
        print(sep.join([headline(jobs), *(format_group(g) for g in group_jobs(jobs))]), flush=True)


class DiscordNotifier(Notifier):
    name = "discord"
    LIMIT = 2000  # Discord message character limit

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url  # never logged

    def build_messages(self, jobs: list[JobPosting]) -> list[str]:
        digest = format_digest(jobs)
        if len(digest) <= self.LIMIT:
            return [digest]
        # One notification even for a large digest. All links go in the attachment.
        return [f"Role Radar — {len(jobs)} new matching jobs\nThe complete list of jobs and application links is attached."]

    async def send(self, jobs: list[JobPosting]) -> None:
        digest = format_digest(jobs)
        content = self.build_messages(jobs)[0]
        payload = {"content": content, "allowed_mentions": {"parse": []}, "flags": 4}
        url = httpx.URL(self._url).copy_merge_params({"wait": "true"})
        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(4):
                if len(digest) > self.LIMIT:
                    resp = await client.post(url, data={"payload_json": json.dumps(payload)},
                                             files={"files[0]": ("role-radar-new-jobs.txt", digest.encode("utf-8"), "text/plain")})
                else:
                    resp = await client.post(url, json=payload)
                if resp.status_code == 429 and attempt < 3:
                    await asyncio.sleep(min(float(resp.json().get("retry_after", 2)), 30))
                    continue
                if not resp.is_success:
                    raise NotificationError(f"Discord webhook returned HTTP {resp.status_code}")
                break


class EmailNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: list[str],
        username: str | None = None,
        password: str | None = None,
        security: str = "starttls",
    ) -> None:
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients
        self._username, self._password = username, password
        self.security = security.strip().lower()
        if self.security not in {"starttls", "ssl", "none"}:
            raise ValueError("SMTP_SECURITY must be starttls, ssl, or none")
        if not recipients:
            raise ValueError("EMAIL_TO must contain at least one recipient")
        if not 1 <= port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if bool(username) != bool(password):
            raise ValueError("Set both SMTP_USERNAME and SMTP_PASSWORD for authenticated email")

    def build_message(self, jobs: list[JobPosting]) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[Role Radar digest] {headline(jobs)}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(format_digest(jobs))
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self._username and self._password:
                server.login(self._username, self._password)
            refused = server.send_message(msg, from_addr=self.sender, to_addrs=self.recipients)
            if refused:
                raise NotificationError(f"SMTP refused {len(refused)} recipient(s)")

    async def send(self, jobs: list[JobPosting]) -> None:
        await asyncio.to_thread(self._send_sync, self.build_message(jobs))


# -- wiring ---------------------------------------------------------------------


class UnconfiguredNotifier(Notifier):
    """Keep one broken channel visible without preventing the other from sending."""

    def __init__(self, name: str, problem: str) -> None:
        self.name, self.problem = name, problem

    async def send(self, jobs: list[JobPosting]) -> None:
        raise ValueError(self.problem)


def notifiers_from_env(env: dict[str, str] | None = None, console_fallback: bool = True) -> list[Notifier]:
    """The channels `env` configures; if none, the console (or nothing, without console_fallback)."""
    env = {k: v if k == "SMTP_PASSWORD" else v.strip() for k, v in (os.environ if env is None else env).items()}
    notifiers: list[Notifier] = []
    if env.get("DISCORD_WEBHOOK_URL"):
        notifiers.append(DiscordNotifier(env["DISCORD_WEBHOOK_URL"]))
    email_keys = ("SMTP_HOST", "EMAIL_TO", "EMAIL_FROM", "SMTP_USERNAME", "SMTP_PASSWORD")
    if any(env.get(key) for key in email_keys):
        security = (env.get("SMTP_SECURITY") or "starttls").lower()
        try:
            missing = [key for key in ("SMTP_HOST", "EMAIL_TO") if not env.get(key)]
            if missing:
                raise ValueError("Missing email settings: " + ", ".join(missing))
            try:
                port = int(env.get("SMTP_PORT") or (465 if security == "ssl" else 587))
            except ValueError:
                raise ValueError("SMTP_PORT must be an integer") from None
            notifiers.append(EmailNotifier(
                host=env["SMTP_HOST"],
                port=port,
                sender=env.get("EMAIL_FROM") or env.get("SMTP_USERNAME") or env["EMAIL_TO"].split(",")[0],
                recipients=[r.strip() for r in env["EMAIL_TO"].split(",") if r.strip()],
                username=env.get("SMTP_USERNAME"),
                password=env.get("SMTP_PASSWORD"),
                security=security,
            ))
        except ValueError as exc:
            notifiers.append(UnconfiguredNotifier("email", str(exc)))
    if not notifiers and console_fallback:
        return [ConsoleNotifier()]
    return notifiers


async def notify_all(
    notifiers: list[Notifier], jobs: list[JobPosting], check: Callable[[], None] | None = None,
    *, records: dict[str, SeenJob] | None = None,
) -> bool:
    """True only when every channel delivered; persist successes per job for retries.

    Successful channels are skipped on subsequent checks. `check` runs
    before each channel and may raise to stop sending (e.g. the lease was lost).
    """
    if not jobs:
        return True
    if not notifiers:
        log.error("No alert channel is configured; %d job(s) stay pending", len(jobs))
        return False
    delivered = True
    for notifier in notifiers:
        pending = [j for j in jobs if records is None or notifier.name not in records[j.uid].notified_channels]
        if not pending:
            continue
        if check:
            check()
        try:
            await notifier.send(pending)
            if records is not None:
                stamp = to_iso(utcnow())
                for job in pending:
                    records[job.uid].notified_channels[notifier.name] = stamp
            log.info("Sent %d job(s) via %s", len(pending), notifier.name)
        except Exception as exc:
            delivered = False
            # Transport errors can contain webhook URLs or SMTP credentials.
            detail = f": {notifier.problem}" if isinstance(notifier, UnconfiguredNotifier) else ""
            if isinstance(exc, NotificationError):
                detail = f": {exc}"
            elif isinstance(exc, smtplib.SMTPResponseException):
                detail = f": SMTP status {exc.smtp_code}"
            log.error("Notification via %s failed (%s)%s", notifier.name, type(exc).__name__, detail)
    return delivered
