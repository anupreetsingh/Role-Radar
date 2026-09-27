"""lambda_handler against fake AWS (moto): S3 config, DynamoDB state and lease, SSM secrets."""

import httpx
import pytest

from role_radar import monitor
from role_radar.http_client import HttpClient
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
    """What the SAM stack provides: the table, the config in S3, the environment variables."""
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="role-radar-config")
    s3.put_object(Bucket="role-radar-config", Key="companies.yaml", Body=CONFIG)
    for name, value in {
        "ROLE_RADAR_STORAGE": "dynamodb",
        "ROLE_RADAR_TABLE": table[1],
        "ROLE_RADAR_CONFIG_URL": "s3://role-radar-config/companies.yaml",
        "ROLE_RADAR_SECRETS": "ssm:/role-radar/",  # empty: alerts go to the log
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(lambda_handler, "_runner", None)  # a cold start
    return table


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


def test_lambda_checks_due_companies_then_releases_the_lease(deployed, sites, capsys):
    summary = lambda_handler.handler({}, FakeContext())
    assert (summary["checked"], summary["alerts"], summary["failed"]) == (1, 2, 0)
    assert "NEW JOB" in capsys.readouterr().out  # no channels configured: printed to the log

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
    assert lambda_handler.handler({}, FakeContext()) == {"skipped": True, "holder": "laptop:mac"}
    assert sites["requests"] == 0


def test_lambda_raises_when_every_company_fails(deployed, sites):
    sites["status"] = 503  # e.g. no network from the function
    with pytest.raises(RuntimeError, match="pass failed"):
        lambda_handler.handler({}, FakeContext())
    client, name = deployed
    assert DynamoLease(client, name, "x").read().released_at  # the lease is still released
