"""Notification channels.

All new jobs from one run go out as a single batch per channel. Jobs with the
same company + title (e.g. one role in several cities) are grouped into one
entry so they don't read as duplicate alerts.

Channels are configured purely from environment variables so secrets never
live in the repo:
  DISCORD_WEBHOOK_URL
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SECURITY, EMAIL_FROM, EMAIL_TO
If none are set, alerts are printed to stdout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from role_radar.models import JobPosting, normalize_text

log = logging.getLogger(__name__)


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

    def __init__(self, webhook_url: str, max_messages: int = 20) -> None:
        self._url = webhook_url  # never logged
        self.max_messages = max_messages

    def build_messages(self, jobs: list[JobPosting]) -> list[str]:
        blocks = [f"**{headline(jobs)}**"] + [f"```\n{format_group(g)}\n```" for g in group_jobs(jobs)]
        messages: list[str] = []
        current = ""
        for block in blocks:
            block = block if len(block) <= self.LIMIT else block[: self.LIMIT - 4] + "…```"
            if current and len(current) + len(block) + 1 > self.LIMIT:
                messages.append(current)
                current = block
            else:
                current = f"{current}\n{block}" if current else block
        if current:
            messages.append(current)
        if len(messages) > self.max_messages:
            dropped = len(messages) - self.max_messages + 1
            messages = messages[: self.max_messages - 1] + [f"…and {dropped} more message(s) of jobs not shown."]
        return messages

    async def send(self, jobs: list[JobPosting]) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            for content in self.build_messages(jobs):
                # allowed_mentions: a job title containing "@everyone" must not ping anyone.
                payload = {"content": content, "allowed_mentions": {"parse": []}}
                for attempt in range(4):
                    resp = await client.post(self._url, json=payload)
                    if resp.status_code == 429 and attempt < 3:
                        await asyncio.sleep(min(float(resp.json().get("retry_after", 2)), 30))
                        continue
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Discord webhook returned HTTP {resp.status_code}")
                    break
                await asyncio.sleep(0.5)  # stay well under webhook rate limits


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
        self.security = security.lower()

    def build_message(self, jobs: list[JobPosting]) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[Role Radar] {headline(jobs)}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(("\n\n" + "=" * 50 + "\n\n").join(format_group(g) for g in group_jobs(jobs)))
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
            server.send_message(msg)

    async def send(self, jobs: list[JobPosting]) -> None:
        await asyncio.to_thread(self._send_sync, self.build_message(jobs))


# -- wiring ---------------------------------------------------------------------


def notifiers_from_env(env: dict[str, str] | None = None) -> list[Notifier]:
    env = dict(os.environ if env is None else env)
    notifiers: list[Notifier] = []
    if env.get("DISCORD_WEBHOOK_URL"):
        notifiers.append(DiscordNotifier(env["DISCORD_WEBHOOK_URL"]))
    if env.get("SMTP_HOST") and env.get("EMAIL_TO"):
        security = env.get("SMTP_SECURITY", "starttls")
        notifiers.append(
            EmailNotifier(
                host=env["SMTP_HOST"],
                port=int(env.get("SMTP_PORT") or (465 if security == "ssl" else 587)),
                sender=env.get("EMAIL_FROM") or env.get("SMTP_USERNAME") or env["EMAIL_TO"].split(",")[0],
                recipients=[r.strip() for r in env["EMAIL_TO"].split(",") if r.strip()],
                username=env.get("SMTP_USERNAME"),
                password=env.get("SMTP_PASSWORD"),
                security=security,
            )
        )
    return notifiers or [ConsoleNotifier()]


async def notify_all(notifiers: list[Notifier], jobs: list[JobPosting]) -> bool:
    """Send via every channel. True if at least one channel delivered.

    "At least one" (rather than "all") avoids re-sending to a working channel
    every run just because another channel is misconfigured.
    """
    if not jobs:
        return True
    delivered = False
    for notifier in notifiers:
        try:
            await notifier.send(jobs)
            delivered = True
            log.info("Sent %d job(s) via %s", len(jobs), notifier.name)
        except Exception as exc:
            # Log only the exception type/message; never the channel config.
            log.error("Notification via %s failed: %s", notifier.name, exc)
    return delivered
