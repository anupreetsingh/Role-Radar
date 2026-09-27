"""lambda_handler against fake AWS (moto): S3 config, DynamoDB state and lease, SSM secrets."""

import time

import httpx
import pytest

from role_radar import monitor
from role_radar.http_client import HttpClient
from role_radar.notifications import DiscordNotifier, EmailNotifier
from tests.conftest import fixture_json

pytest.importorskip("moto")

import lambda_handler  # noqa: E402
from role_radar.dynamo import DynamoLease, DynamoStateStore  # noqa: E402

CONFIG = b"""
settings:
  http: {per_domain_delay: 0, respect_robots: false, max_retries: 0}
companies:
  - name: Continental Finance
    url: https://contfinco.bamboohr.com/careers
    filters: {include_keywords: [software developer, data engineer]}
"""


class FakeContext:
    aws_request_id = "3f1c9a7e-0000-0000-0000-000000000000"

    def get_remaining_time_in_millis(self):
        return 840_000


@pytest.fixture
def deployed(table, monkeypatch):
    """What the SAM stack provides (table, config bucket, environment), plus the pushed config."""
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="role-radar-config")
    s3.put_object(Bucket="role-radar-config", Key="companies.yaml", Body=CONFIG)
    for name, value in {
        "ROLE_RADAR_STORAGE": "dynamodb",
        "ROLE_RADAR_TABLE": table[1],
        "ROLE_RADAR_CONFIG_URL": "s3://role-radar-config/companies.yaml",
        "ROLE_RADAR_SECRETS": "ssm:/role-radar/",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(lambda_handler, "_runner", None)  # a cold start
    return table


@pytest.fixture
def discord(fake_aws, monkeypatch):
    """A Discord webhook in SSM; messages are captured instead of sent."""
    import boto3

    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/hook", Type="SecureString"
    )
    sent = []

    async def send(self, jobs):
        sent.append([j.uid for j in jobs])

    monkeypatch.setattr(DiscordNotifier, "send", send)
    return sent


@pytest.fixture
def sites(monkeypatch):
    """Every careers-site request, answered from fixtures (status per test)."""
    state = {"status": 200, "requests": 0}

    def handler(request):
        state["requests"] += 1
        return httpx.Response(state["status"], json=fixture_json("bamboohr_list.json"))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(monitor, "HttpClient", lambda s, **kw: HttpClient(s, transport=transport, **kw))
    return state


def test_lambda_checks_due_companies_then_releases_the_lease(deployed, sites, discord):
    summary = lambda_handler.handler({}, FakeContext())
    assert (summary["checked"], summary["alerts"], summary["failed"]) == (1, 2, 0)
    assert len(discord) == 1 and len(discord[0]) == 2

    client, name = deployed
    info = DynamoLease(client, name, "laptop:mac").read()
    assert info.holder == "lambda:3f1c9a7e" and info.released_at  # free again straight away
    store = DynamoStateStore(client, name)
    assert store.last_runs()["lambda"]["alerts"] == 2
    assert len(store.recent_alerts()) == 2

    again = lambda_handler.handler({}, FakeContext())  # 5 minutes later: nothing due
    assert again["checked"] == 0 and sites["requests"] == 1


def test_lambda_exits_at_once_while_the_laptop_holds_the_lease(deployed, sites):
    client, name = deployed
    DynamoLease(client, name, "laptop:mac").acquire(180)
    assert lambda_handler.handler({}, FakeContext()) == {"skipped": True, "reason": "laptop:mac holds the lease"}
    assert sites["requests"] == 0


def test_cold_lambda_sends_due_digest_even_without_due_company(deployed, sites, discord, monkeypatch):
    import boto3

    config = CONFIG.replace(b"settings:\n", b"settings:\n  digest_interval_minutes: 30\n  check_interval_minutes: 1440\n")
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket="role-radar-config", Key="companies.yaml", Body=config,
    )
    first = lambda_handler.handler({}, FakeContext())
    assert first["checked"] == 1 and first["alerts"] == 0 and not discord
    store = DynamoStateStore(*deployed)
    schedule = store.load_digest()
    assert schedule.interval_minutes == 30 and schedule.next_send_at
    schedule.next_send_at = "2000-01-01T00:00:00Z"
    store.save_digest(schedule)

    monkeypatch.setattr(lambda_handler, "_runner", None)
    second = lambda_handler.handler({}, FakeContext())
    assert second["checked"] == 0 and second["alerts"] == 2
    assert second["digest_attempted"] and second["digest_jobs"] == 2
    assert sites["requests"] == 1 and len(discord) == 1 and len(discord[0]) == 2
    assert store.last_runs()["lambda"]["digest_attempted"]
    assert store.load_digest().next_send_at != schedule.next_send_at
    lambda_handler.handler({}, FakeContext())
    assert len(discord) == 1


def test_lambda_idles_until_the_config_is_pushed(deployed, sites):
    import boto3

    boto3.client("s3", region_name="us-east-1").delete_object(Bucket="role-radar-config", Key="companies.yaml")
    result = lambda_handler.handler({}, FakeContext())  # no error, so no alarm during setup
    assert result["skipped"] and "role-radar config push" in result["reason"]
    assert sites["requests"] == 0 and DynamoLease(deployed[0], deployed[1], "x").read() is None


def test_missing_secrets_keep_alerts_pending_and_trip_the_alarm(deployed, sites, discord, monkeypatch):
    import boto3

    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.delete_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL")  # e.g. misnamed
    with pytest.raises(RuntimeError, match="undelivered"):
        lambda_handler.handler({}, FakeContext())
    record = DynamoStateStore(*deployed).load_company("Continental Finance")
    assert not any(j.notified_at for j in record.jobs.values())  # nothing silently marked as sent

    ssm.put_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/hook", Type="SecureString")
    monkeypatch.setattr(lambda_handler._runner, "clock", lambda: time.time() + 3600)  # an hour later: due again
    summary = lambda_handler.handler({}, FakeContext())
    assert summary["alerts"] == 2 and len(discord) == 1


def test_lambda_raises_when_every_company_fails(deployed, sites):
    sites["status"] = 503  # e.g. no network from the function
    with pytest.raises(RuntimeError, match="pass failed"):
        lambda_handler.handler({}, FakeContext())
    client, name = deployed
    assert DynamoLease(client, name, "x").read().released_at  # the lease is still released


def test_partial_delivery_retries_only_failed_channel_after_cold_start(deployed, sites, discord, monkeypatch):
    import boto3

    ssm = boto3.client("ssm", region_name="us-east-1")
    for key, value in {"SMTP_HOST": "smtp.invalid", "EMAIL_TO": "a@example.com"}.items():
        ssm.put_parameter(Name=f"/role-radar/{key}", Value=value, Type="SecureString")

    async def fail(self, jobs):
        raise RuntimeError("SMTP unavailable")

    monkeypatch.setattr(EmailNotifier, "send", fail)
    with pytest.raises(RuntimeError, match="undelivered"):
        lambda_handler.handler({}, FakeContext())
    store = DynamoStateStore(*deployed)
    record = store.load_company("Continental Finance")
    pending = [j for j in record.jobs.values() if j.matched]
    assert len(pending) == 2 and all(j.notified_channels.keys() == {"discord"} and not j.notified_at for j in pending)
    assert store.last_runs()["lambda"]["undelivered"] == 1

    sent = []

    async def recovered(self, jobs):
        sent.extend(jobs)

    monkeypatch.setattr(EmailNotifier, "send", recovered)
    monkeypatch.setattr(lambda_handler, "_runner", None)  # a different Lambda / laptop process
    record.meta.next_check_at = "2000-01-01T00:00:00Z"
    store.save_company(record)
    summary = lambda_handler.handler({}, FakeContext())
    assert summary["undelivered"] == 0 and len(sent) == 2 and len(discord) == 1
    assert all(j.notified_at and j.notified_channels.keys() == {"discord", "email"}
               for j in store.load_company("Continental Finance").jobs.values() if j.matched)
