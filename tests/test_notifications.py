import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from role_radar import notifications
from role_radar.notifications import DiscordNotifier, EmailNotifier, UnconfiguredNotifier, format_digest, notifiers_from_env, notify_all
from tests.conftest import job


def test_large_discord_digest_uses_one_message():
    jobs = [job(f"Software Engineer {i}", str(i), location="L" * 300) for i in range(180)]
    messages = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert len(messages) == 1
    assert all(len(message) <= 2000 for message in messages)
    assert "attached" in messages[0]
    assert all(j.url in format_digest(jobs) for j in jobs)


def test_oversized_group_keeps_all_locations_and_links():
    jobs = [job("Software Engineer", str(i), location=f"City {i}") for i in range(180)]
    messages = DiscordNotifier("https://discord.invalid/webhook").build_messages(jobs)
    assert all(len(message) <= 2000 for message in messages)
    assert len(messages) == 1
    assert all(j.url in format_digest(jobs) for j in jobs)


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


def test_large_discord_digest_uploads_all_links_in_one_request(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "123"})

    real = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    jobs = [job(f"Software Engineer {i}", str(i), location="L" * 100) for i in range(80)]
    asyncio.run(DiscordNotifier("https://discord.invalid/hook").send(jobs))
    assert len(requests) == 1
    body = requests[0].content.decode()
    assert "multipart/form-data" in requests[0].headers["content-type"]
    assert 'name="files[0]"' in body and 'filename="role-radar-new-jobs.txt"' in body
    assert all(j.url in body for j in jobs)
    assert '"allowed_mentions": {"parse": []}' in body


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
