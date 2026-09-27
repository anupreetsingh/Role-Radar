"""Scraper interface and shared parsing helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar, Mapping
from urllib.parse import urlsplit

from role_radar.config import CompanyConfig
from role_radar.http_client import HttpClient
from role_radar.models import JobPosting
from role_radar.storage import SeenJob


@dataclass
class ScrapeResult:
    jobs: list[JobPosting] = field(default_factory=list)
    # False when the listing may be partial (page cap hit, heuristic parse),
    # in which case missing jobs must NOT be treated as removed.
    complete: bool = True
    # The listing's newest page, from scrapers that support quick checks.
    first_page: list[JobPosting] | None = None


class ScraperError(Exception):
    """Raised when a careers page can't be parsed into jobs."""


class BaseScraper(ABC):
    name: ClassVar[str]
    # Hostname suffixes used to auto-detect this ATS from a careers URL.
    domains: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: CompanyConfig, http: HttpClient) -> None:
        self.company = company
        self.http = http
        self.options = company.options
        # uid → the stored record of each job seen before (read-only), for scrapers
        # that can reuse what's known instead of fetching it again.
        self.known: Mapping[str, SeenJob] = {}

    @classmethod
    def handles_url(cls, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in cls.domains)

    @abstractmethod
    async def fetch_jobs(self) -> ScrapeResult:
        """Return every currently listed job for the company."""

    # Quick checks (settings.quick_check_by_ats), for listings that are newest first.
    supports_quick = False

    async def fetch_first_page(self) -> list[JobPosting]:
        """The listing's newest page, for a quick check."""
        raise NotImplementedError(f"{self.name} doesn't support quick checks")

    async def fetch_newest(self, first_page: list[JobPosting]) -> ScrapeResult:
        """The jobs newer than any in self.known, reading on from `first_page` (a partial listing)."""
        raise NotImplementedError(f"{self.name} doesn't support quick checks")

    def missing_fields(self, job: JobPosting) -> set[str]:
        """Filter fields the listing lacks for `job` that fetch_details() would fill in. Default: none."""
        return set()

    def wants_details(self, job: JobPosting) -> bool:
        """True when the company's filter depends on a field only the job's own page has."""
        return bool(self.missing_fields(job) & self.company.filter.fields_used())

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        """Fill in fields only available from the job's own page. Default: no-op."""
        return job

    def make_job(self, **kwargs: Any) -> JobPosting:
        return JobPosting(company=self.company.name, source=self.name, **kwargs)


# -- helpers ----------------------------------------------------------------

_RELATIVE = re.compile(r"(\d+)\+?\s+(day|week|month)s?\s+ago", re.I)


def parse_date(value: Any, today: date | None = None) -> date | None:
    """Best-effort parse of the many date formats ATSs return.

    Handles ISO 8601 strings, epoch milliseconds/seconds, and Workday-style
    relative strings ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago").
    """
    if value is None or value == "":
        return None
    today = today or datetime.now(timezone.utc).date()
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    text = str(value).strip()
    if text.isdigit():
        return parse_date(int(text), today)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    named = _named_month_date(text)
    if named:
        return named
    lowered = text.lower()
    if "today" in lowered or "just posted" in lowered:
        return today
    if "yesterday" in lowered:
        return today - timedelta(days=1)
    m = _RELATIVE.search(lowered)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return today - timedelta(days=days)
    return None


def _named_month_date(text: str) -> date | None:
    """ "September 25, 2026" (amazon.jobs) or "Sep 27, 2026"."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def join_nonempty(*parts: str | None, sep: str = ", ") -> str | None:
    cleaned = [p.strip() for p in parts if p and str(p).strip()]
    return sep.join(dict.fromkeys(cleaned)) or None


def first_path_segment(url: str) -> str | None:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return segments[0] if segments else None
