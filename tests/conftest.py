from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

from config import CompanyConfig
from filters import JobFilter
from http_client import DEFAULT_HOST_DELAYS, HttpClient, HttpSettings
from models import JobPosting

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
