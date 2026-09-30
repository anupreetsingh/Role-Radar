import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from role_radar import notifications
from role_radar.notifications import (
    ConsoleNotifier, DiscordNotifier, EmailNotifier, UnconfiguredNotifier, format_digest, notifiers_from_env, notify_all,
    switched_on,
)
from tests.conftest import job


def within_discord_limits(payload):
    embeds = payload["embeds"]
    assert len(payload["content"]) <= 2000 and 1 <= len(embeds) <= 10
    assert all(len(e["description"]) <= 4096 for e in embeds)
    assert sum(len(e["description"]) for e in embeds) <= 6000
    return True


def text_of(payload):
    return "\n".join(e["description"] for e in payload["embeds"])


def test_small_discord_digest_links_every_job_in_one_message():
    jobs = [job(f"Software Engineer {i}", str(i)) for i in range(20)] + [job("Data Engineer", "d", company="Beta")]
    payload = DiscordNotifier("https://discord.invalid/webhook").build_message(jobs)
    assert within_discord_limits(payload)
    assert payload["content"] == "**Role Radar** — 21 new matching jobs — Acme, Beta"
    assert all(f"[{j.title}]({j.url}) — **{j.company}** · Austin, TX" in text_of(payload) for j in jobs)
    assert "more" not in text_of(payload)
    lines = text_of(payload).splitlines()
    assert "[Data Engineer]" in lines[-1]  # in the order given (a digest's newest first), not by company


def test_large_discord_digest_fills_one_message_and_counts_the_rest():
    jobs = [job(f"Software Engineer {i:03}", str(i), location="L" * 100) for i in range(180)]
    payload = DiscordNotifier("https://discord.invalid/webhook").build_message(jobs)
    assert within_discord_limits(payload)
    shown = [j for j in jobs if f"({j.url})" in text_of(payload)]
    assert len(shown) > 20
    assert f"**…and {len(jobs) - len(shown)} more.**" in text_of(payload)
    assert "the email has every job" in text_of(payload)


def test_a_role_in_many_places_links_the_first_few():
    jobs = [job("Software Engineer", str(i), location=f"City {i}") for i in range(180)]
    payload = DiscordNotifier("https://discord.invalid/webhook").build_message(jobs)
    assert within_discord_limits(payload)
    assert "• **Software Engineer** — **Acme** · [City 0](https://acme.example/jobs/0) · [City 1](" in text_of(payload)
    assert text_of(payload).count("](https://acme.example/jobs/") == 8
    assert "+172 more (in the email)" in text_of(payload) and "…and" not in text_of(payload)


def test_discord_without_email_sends_every_job_over_several_messages():
    jobs = [job(f"Software Engineer {i:03}", str(i), location="L" * 100) for i in range(180)]
    payloads = DiscordNotifier("https://discord.invalid/webhook").alone().build_messages(jobs)
    assert len(payloads) > 1 and all(within_discord_limits(p) for p in payloads)
    text = "\n".join(text_of(p) for p in payloads)
    assert all(text.count(f"({j.url})") == 1 for j in jobs) and "email" not in text
    assert all(line.startswith("• ") and "**Acme**" in line for p in payloads for line in text_of(p).splitlines())
    assert [p["content"][-5:] for p in payloads[:2]] == [f"(1/{len(payloads)})", f"(2/{len(payloads)})"]

    many_places = [job("Software Engineer", str(i), location=f"City {i}") for i in range(180)]
    (payload,) = DiscordNotifier("https://discord.invalid/webhook").alone().build_messages(many_places)
    assert "+172 more" in text_of(payload) and "email" not in text_of(payload)


def test_only_the_channels_switched_on_send():
    discord = DiscordNotifier("https://discord.invalid/hook")
    email = EmailNotifier("smtp.invalid", 587, "from@example.com", ["to@example.com"])
    assert switched_on([discord, email], {}) == [discord, email]
    assert switched_on([discord, email], {"discord": False}) == [email]
    (alone,) = switched_on([discord, email], {"email": False})
    assert alone.name == "discord" and alone.every_job and not discord.every_job
    assert switched_on([discord], {})[0].every_job  # no email set up: Discord carries every job too
    assert [c.name for c in switched_on([ConsoleNotifier()], {"discord": False, "email": False})] == ["console"]


def test_discord_escapes_markdown_and_keeps_links_intact():
    jobs = [job("C++ Engineer [Senior] *Remote*", "1", location="Austin_TX", url="https://x.example/a job (1)")]
    payload = DiscordNotifier("https://discord.invalid/webhook").build_message(jobs)
    assert "[C++ Engineer \\[Senior\\] \\*Remote\\*](https://x.example/a%20job%20%281%29) — **Acme** · Austin\\_TX" in text_of(payload)


def test_discord_waits_for_confirmation_and_retries_rate_limit(monkeypatch):
    requests = []
    waits = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, json={"retry_after": 1}) if len(requests) == 1 else httpx.Response(200, json={"id": "123"})

    async def sleep(seconds):
        waits.append(seconds)

    real = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(notifications.asyncio, "sleep", sleep)
    asyncio.run(DiscordNotifier("https://discord.invalid/hook?thread_id=42&wait=false").send([job()]))
    assert len(requests) == 2 and waits == [1]
    assert all(r.url.params["wait"] == "true" and r.url.params["thread_id"] == "42" for r in requests)
    assert json.loads(requests[-1].content)["allowed_mentions"] == {"parse": []}


def test_large_discord_digest_is_one_json_message(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "123"})

    real = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    jobs = [job(f"Software Engineer {i}", str(i), location="L" * 100) for i in range(80)]
    asyncio.run(DiscordNotifier("https://discord.invalid/hook").send(jobs))
    assert len(requests) == 1 and requests[0].headers["content-type"] == "application/json"
    sent = json.loads(requests[0].content)
    assert sent["allowed_mentions"] == {"parse": []} and "more.**" in text_of(sent)

    requests.clear()
    asyncio.run(DiscordNotifier("https://discord.invalid/hook").alone().send(jobs))
    assert len(requests) > 1 and all(f"({j.url})" in "\n".join(text_of(json.loads(r.content)) for r in requests) for j in jobs)


def test_email_digest_lists_jobs_from_every_company():
    jobs = [job(company="Acme"), job("Backend Engineer", "2", company="Other")]
    msg = EmailNotifier("smtp.invalid", 587, "from@example.com", ["to@example.com"]).build_message(jobs)
    assert "digest" in msg["Subject"]
    assert all(j.url in msg.get_content() and j.company in msg.get_content() for j in jobs)


def test_email_digest_lists_roles_in_the_order_given_each_with_its_company():
    jobs = [job("Backend Engineer", "2", company="Zeta", location="Remote"),  # newest
            job("Software Engineer", "1", company="Acme", location="Austin, TX"),
            job("Software Engineer", "3", company="Acme", location="Boston, MA")]
    text = format_digest(jobs)
    assert text.split("\n", 2)[1] == "3 new matching jobs — Zeta, Acme"
    assert text.split("\n")[2:] == [
        "", "Backend Engineer — Zeta", "Remote", "https://acme.example/jobs/2",
        "", "Software Engineer — Acme", "Austin, TX | Boston, MA", "https://acme.example/jobs/1", "https://acme.example/jobs/3",
    ]


@pytest.mark.parametrize("extra,problem", [
    ({"EMAIL_TO": ""}, "EMAIL_TO"),
    ({"SMTP_SECURITY": "TLS-typo"}, "SMTP_SECURITY"),
    ({"SMTP_PORT": "secret-invalid-value"}, "SMTP_PORT"),
    ({"SMTP_USERNAME": "user"}, "SMTP_PASSWORD"),
    ({"EMAIL_TO": ", ,"}, "recipient"),
])
def test_broken_email_does_not_disable_discord_or_fall_back_to_console(extra, problem):
    env = {"DISCORD_WEBHOOK_URL": "https://discord.invalid/hook", "SMTP_HOST": "smtp.invalid", "EMAIL_TO": "a@example.com", **extra}
    discord, email = notifiers_from_env(env)
    assert isinstance(discord, DiscordNotifier)
    assert isinstance(email, UnconfiguredNotifier) and problem in email.problem
    assert "secret-invalid-value" not in email.problem


def test_ssl_settings_are_normalized_before_choosing_port():
    (email,) = notifiers_from_env({"SMTP_HOST": " smtp.invalid ", "EMAIL_TO": " a@example.com ", "SMTP_SECURITY": " SSL "})
    assert (email.port, email.security, email.host) == (465, "ssl", "smtp.invalid")


def test_smtp_password_is_not_modified():
    (email,) = notifiers_from_env({"SMTP_HOST": "smtp.invalid", "EMAIL_TO": "a@example.com",
                                  "SMTP_USERNAME": "a@example.com", "SMTP_PASSWORD": " spaces matter "})
    assert email._password == " spaces matter "


def test_smtp_partial_recipient_refusal_is_a_failure(monkeypatch):
    server = MagicMock()
    server.__enter__.return_value = server
    server.send_message.return_value = {"b@example.com": (550, b"refused")}
    monkeypatch.setattr(notifications.smtplib, "SMTP", lambda *a, **kw: server)
    notifier = EmailNotifier("smtp.invalid", 587, "from@example.com", ["a@example.com", "b@example.com"])
    with pytest.raises(RuntimeError, match="refused 1 recipient"):
        asyncio.run(notifier.send([job()]))
    assert server.send_message.call_args.kwargs["to_addrs"] == ["a@example.com", "b@example.com"]
    assert server.send_message.call_args.args[0]["To"] == "a@example.com"  # b never sees a's address, nor a b's


def test_transport_errors_do_not_log_webhook_secrets(monkeypatch, caplog):
    secret = "https://discord.invalid/webhook/private-token"

    async def send(self, jobs):
        raise httpx.ConnectError(f"Could not connect to {secret}")

    monkeypatch.setattr(DiscordNotifier, "send", send)
    assert not asyncio.run(notify_all([DiscordNotifier(secret)], [job()]))
    assert "ConnectError" in caplog.text and "private-token" not in caplog.text
