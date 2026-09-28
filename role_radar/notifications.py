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

Discord and email each have an on/off switch (`role-radar switch discord off`,
or the menu bar app). Alerts go only to the channels switched on; with both
off, matches stay pending and go out in the first digest after one is back on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Sequence

import httpx

from role_radar.models import JobPosting, normalize_text
from role_radar.storage import SeenJob, switch_on, to_iso, utcnow

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
    """The digest as one Discord message: every job a clickable link, grouped by company.

    A message holds about 6,000 characters of embeds (roughly 40-60 jobs). A digest
    that doesn't fit shows what does and says how many more are in the email.
    With `every_job` (email is off), the rest go in more messages instead.
    """

    name = "discord"
    CONTENT_LIMIT = 2000  # a message's own text
    EMBED_TEXT = 4000  # an embed description holds 4,096 characters
    MESSAGE_TEXT = 5850  # all of a message's embeds together hold 6,000, so it's 2 embeds at most
    COLOR = 0x5865F2

    def __init__(self, webhook_url: str, *, every_job: bool = False) -> None:
        self._url = webhook_url  # never logged
        self.every_job = every_job

    def alone(self) -> DiscordNotifier:
        """This channel for when no email goes out: a big digest takes more messages, not a pointer to the email."""
        return DiscordNotifier(self._url, every_job=True)

    def build_message(self, jobs: list[JobPosting]) -> dict:
        """The webhook payload for a digest that fits one message (the first message of a bigger one)."""
        return self.build_messages(jobs)[0]

    def build_messages(self, jobs: list[JobPosting]) -> list[dict]:
        """The webhook payloads for one digest: one message, or with `every_job` as many as it takes."""
        pages: list[list[str]] = [[]]
        size, company, shown = 0, None, 0
        for group_company, group_lines, count in _discord_groups(jobs, every_job=self.every_job):
            header = ["", f"__**{_md(group_company)}**__"]
            block = group_lines if group_company == company else [*header, *group_lines]
            cost = sum(len(text) + 1 for text in block)
            if size + cost > self.MESSAGE_TEXT - 100 and self.every_job and pages[-1]:
                pages.append([])  # a new message repeats the company's name
                block, size = [*header, *group_lines], 0
                cost = sum(len(text) + 1 for text in block)
            if size + cost > self.MESSAGE_TEXT - 100:  # room for the "more" line; smaller groups may still fit
                continue
            pages[-1] += block
            size += cost
            company, shown = group_company, shown + count
        if shown < len(jobs):
            pages[-1] += ["", f"**…and {len(jobs) - shown} more.** That's all one Discord message holds; the email has every job."]
        title = f"**Role Radar** — {_md(headline(jobs))}"
        return [
            {"content": (title if len(pages) == 1 else f"{title} ({i}/{len(pages)})")[: self.CONTENT_LIMIT],
             "embeds": [{"description": text, "color": self.COLOR} for text in _chunks(lines, self.EMBED_TEXT)],
             "allowed_mentions": {"parse": []}}
            for i, lines in enumerate(pages, 1)
        ]

    async def send(self, jobs: list[JobPosting]) -> None:
        # A failure part-way raises, so the whole digest is sent again next time.
        url = httpx.URL(self._url).copy_merge_params({"wait": "true"})
        async with httpx.AsyncClient(timeout=20) as client:
            for payload in self.build_messages(jobs):
                for attempt in range(4):
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 429 and attempt < 3:
                        await asyncio.sleep(min(float(resp.json().get("retry_after", 2)), 30))
                        continue
                    if not resp.is_success:
                        raise NotificationError(f"Discord webhook returned HTTP {resp.status_code}")
                    break


MAX_PLACES = 8  # location links shown for one role posted in many places


def _discord_groups(jobs: list[JobPosting], *, every_job: bool = False):
    """(company, lines, job count) for each job group: its title linking to the job, then
    its locations; a group whose locations have their own links links each location."""
    for group in group_jobs(jobs):
        urls = list(dict.fromkeys(j.url for j in group.jobs))
        if len(urls) == 1:
            line = f"• {_link(group.title, urls[0])} — {_md(_clip(' | '.join(group.locations), 120))}"
            yield group.company, [line], len(group.jobs)
            continue
        links: dict[str, str] = {}
        for job in group.jobs:
            links.setdefault(job.url, job.location or "Apply")
        places = [_link(place, url) for url, place in list(links.items())[:MAX_PLACES]]
        if len(links) > MAX_PLACES:
            places.append(f"+{len(links) - MAX_PLACES} more" + ("" if every_job else " (in the email)"))
        yield group.company, [f"• **{_md(_clip(group.title, 150))}** — {' · '.join(places)}"], len(group.jobs)


def _chunks(lines: list[str], limit: int) -> list[str]:
    """Lines joined into texts of at most `limit` characters, never splitting a line."""
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1
    return ["\n".join(chunk).strip("\n") or "\u200b" for chunk in chunks]


def _link(text: str, url: str) -> str:
    # Masked-link targets can't hold spaces or unbalanced parentheses.
    target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{_md(_clip(text, 150))}]({target})"


def _md(text: str) -> str:
    """Text with Discord markdown characters escaped, so titles show as written."""
    return re.sub(r"([\\*_~`|\[\]>])", r"\\\1", text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def switched_on(channels: Sequence[Notifier], switches: dict[str, bool]) -> list[Notifier]:
    """The channels whose switch is on (others, like the console, have no switch).

    Without email, Discord sends every job of a big digest itself.
    """
    on = [channel for channel in channels if switch_on(switches, channel.name)]
    if not any(channel.name == "email" for channel in on):
        on = [channel.alone() if isinstance(channel, DiscordNotifier) else channel for channel in on]
    return on


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
