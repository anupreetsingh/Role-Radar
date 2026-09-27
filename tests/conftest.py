from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

import httpx
import pytest

from role_radar.config import CompanyConfig
from role_radar.filters import JobFilter
from role_radar.http_client import DEFAULT_HOST_DELAYS, HttpClient, HttpSettings
from role_radar.models import JobPosting

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


def make_client(handler: Callable[[httpx.Request], httpx.Response], **overrides) -> HttpClient:
    options = {"per_domain_delay": 0, "respect_robots": False, "max_retries": 2, "backoff_base": 0, **overrides}
    # Zero delays for the built-in API hosts too, unless a test sets its own.
    options["host_delays"] = {host: 0 for host in DEFAULT_HOST_DELAYS} | options.get("host_delays", {})
    return HttpClient(HttpSettings(**options), transport=httpx.MockTransport(handler))


def company(name: str = "Acme", url: str = "https://acme.example/careers", **filters) -> CompanyConfig:
    return CompanyConfig(name=name, url=url, filter=JobFilter(**filters))


def job(title: str = "Software Engineer", job_id: str | None = "1", location: str | None = "Austin, TX", **kw) -> JobPosting:
    kw.setdefault("url", f"https://acme.example/jobs/{job_id or title.replace(' ', '-')}")
    return JobPosting(company=kw.pop("company", "Acme"), title=title, job_id=job_id, location=location, source="test", **kw)


@pytest.fixture
def make_job():
    return job


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path_factory):
    """Keep ROLE_RADAR_* settings from the developer's shell out of the tests, and
    ~/.role-radar untouched; undo cli.setup_logging()'s changes to the root logger."""
    for name in [n for n in os.environ if n.startswith("ROLE_RADAR_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("ROLE_RADAR_HOME", str(tmp_path_factory.mktemp("role-radar-home")))
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


class Clock:
    """A settable stand-in for time.time()."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_aws(monkeypatch):
    """Fake AWS (moto) with dummy credentials, so no test can reach a real account."""
    moto = pytest.importorskip("moto")
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        yield


@pytest.fixture
def table(fake_aws):
    """A fresh Role Radar table in fake DynamoDB; returns (client, table name)."""
    import boto3

    from role_radar.dynamo import create_table

    client = boto3.client("dynamodb", region_name="us-east-1")
    create_table(client, "role-radar-test")
    return client, "role-radar-test"
