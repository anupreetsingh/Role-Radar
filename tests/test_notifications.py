import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from role_radar import notifications
from role_radar.notifications import DiscordNotifier, EmailNotifier, UnconfiguredNotifier, format_digest, notifiers_from_env, notify_all
from tests.conftest import job


def within_discord_limits(payloads):
    for payload in payloads:
        embeds = payload["embeds"]
        assert len(payload.get("content", "")) <= 2000 and 1 <= len(embeds) <= 10
        assert all(len(e["description"]) <= 4096 for e in embeds)
        assert sum(len(e["description"]) + len(e.get("footer", {}).get("text", "")) for e in embeds) <= 6000
    return True


def text_of(payloads):
    return "\n".join(e["description"] for p in payloads for e in p["embeds"])


def test_large_discord_digest_is_split_into_silent_follow_ups_with_every_link():
    jobs = [job(f"Software Engineer {i}", str(i), location="L" * 300) for i in range(180)]
    payloads = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert len(payloads) > 1 and within_discord_limits(payloads)
    assert all(f"[Software Engineer {i}]({j.url})" in text_of(payloads) for i, j in enumerate(jobs))
    assert payloads[0]["content"].startswith("**Role Radar** — 180 new matching jobs") and "flags" not in payloads[0]
    assert all(p["flags"] == DiscordNotifier.SILENT and "content" not in p for p in payloads[1:])
    assert payloads[-1]["embeds"][-1]["footer"]["text"] == f"Part {len(payloads)} of {len(payloads)}"
    assert all(p["embeds"][0]["description"].startswith("__**Acme**__") for p in payloads)  # company repeated


def test_oversized_group_links_every_location():
    jobs = [job("Software Engineer", str(i), location=f"City {i}") for i in range(180)]
    payloads = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert within_discord_limits(payloads)
    assert all(f"[City {i}]({j.url})" in text_of(payloads) for i, j in enumerate(jobs))


def test_discord_escapes_markdown_and_keeps_links_intact():
    jobs = [job("C++ Engineer [Senior] *Remote*", "1", location="Austin_TX", url="https://x.example/a job (1)")]
    (payload,) = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert "[C++ Engineer \\[Senior\\] \\*Remote\\*](https://x.example/a%20job%20%281%29) — Austin\\_TX" in text_of([payload])


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


def test_large_discord_digest_sends_every_part_as_json(monkeypatch):
    requests, waits = [], []

    def handle(request):
        requests.append(request)
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.5"} if len(requests) == 1 else {}
        return httpx.Response(200, json={"id": "123"}, headers=headers)

    async def sleep(seconds):
        waits.append(seconds)

    real = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(notifications.asyncio, "sleep", sleep)
    jobs = [job(f"Software Engineer {i}", str(i), location="L" * 100) for i in range(80)]
    asyncio.run(DiscordNotifier("https://discord.invalid/hook").send(jobs))
    assert len(requests) > 1 and waits == [0.5]  # paced by Discord's rate-limit headers
    assert all(r.headers["content-type"] == "application/json" for r in requests)
    sent = [json.loads(r.content) for r in requests]
    assert all(j.url in text_of(sent) for j in jobs)
    assert all(p["allowed_mentions"] == {"parse": []} for p in sent)


def test_email_digest_lists_jobs_from_every_company():
    jobs = [job(company="Acme"), job("Backend Engineer", "2", company="Other")]
    msg = EmailNotifier("smtp.invalid", 587, "from@example.com", ["to@example.com"]).build_message(jobs)
    assert "digest" in msg["Subject"]
    assert all(j.url in msg.get_content() and j.company in msg.get_content() for j in jobs)


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


def test_transport_errors_do_not_log_webhook_secrets(monkeypatch, caplog):
    secret = "https://discord.invalid/webhook/private-token"

    async def send(self, jobs):
        raise httpx.ConnectError(f"Could not connect to {secret}")

    monkeypatch.setattr(DiscordNotifier, "send", send)
    assert not asyncio.run(notify_all([DiscordNotifier(secret)], [job()]))
    assert "ConnectError" in caplog.text and "private-token" not in caplog.text
