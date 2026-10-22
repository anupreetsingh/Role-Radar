"""Dayforce (Ceridian) career sites (WestJet, Northland Properties, Coast Hotels...), via the job search
their own pages call.

Careers URL: https://jobs.dayforcehcm.com/[{culture}/]{client}/{board} (the board is often
CANDIDATEPORTAL), or an older https://{pod}.dayforcehcm.com/CandidatePortal/{culture}/{client}[/Site/{board}],
which now redirects there.
  GET  https://jobs.dayforcehcm.com/api/auth/csrf
       the token the site's pages send with every search, with the cookie it belongs to
  POST https://jobs.dayforcehcm.com/api/geo/{client}/jobposting/search
       {"clientNamespace", "jobBoardCode", "cultureCode", "paginationStart"}: 25 jobs a page, newest
       first, each with its places and whole description (the experience check needs nothing more)
A job's page is https://jobs.dayforcehcm.com/{culture}/{client}/{board}/jobs/{id}.
Options: client, board, culture, max_jobs (default 1000).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import COUNTRY_NAMES, BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

SITE = "https://jobs.dayforcehcm.com"
QUICK_MAX_PAGES = 4  # a quick check reads at most this many pages; a full check gets the rest
_CULTURE = re.compile(r"^[a-z]{2}(?:-[A-Za-z0-9]{2,4})?$")


def board_of(url: str) -> tuple[str | None, str | None, str | None]:
    """(culture, client, board) in a Dayforce careers URL; parts it doesn't name are None."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments and segments[0].lower() == "candidateportal":  # /CandidatePortal/en-US/coasthotels[/Site/X]
        segments = segments[1:]
        culture = segments.pop(0) if segments and _CULTURE.match(segments[0]) else None
        client = segments[0] if segments else None
        board = segments[2] if len(segments) > 2 and segments[1].lower() == "site" else None
        return culture, client, board
    culture = segments.pop(0) if segments and _CULTURE.match(segments[0]) else None
    return culture, (segments[0] if segments else None), (segments[1] if len(segments) > 1 else None)


class DayforceScraper(BaseScraper):
    name = "dayforce"
    domains = ("dayforcehcm.com",)
    supports_quick = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        culture, client, board = board_of(self.company.url)
        self.client = str(self.options.get("client") or client or "").lower()  # "WestJet" in links, "westjet" to the API
        if not self.client:
            raise ScraperError("could not determine the Dayforce client from the URL; set options.client")
        self.board = str(self.options.get("board") or board or "CANDIDATEPORTAL")
        self.culture = str(self.options.get("culture") or culture or "en-US")
        self._session: dict[str, str] | None = None

    async def fetch_jobs(self) -> ScrapeResult:
        max_jobs = int(self.options.get("max_jobs", 1000))
        jobs: dict[str, JobPosting] = {}
        first_page: list[JobPosting] | None = None
        offset = 0
        while offset < max_jobs:
            page, total = await self._page(offset)
            first_page = page if first_page is None else first_page
            for job in page:
                jobs.setdefault(job.uid, job)  # pages can overlap while jobs churn
            offset += len(page)
            if not page or offset >= total:
                return ScrapeResult(list(jobs.values()), first_page=first_page)
        return ScrapeResult(list(jobs.values()), complete=False, first_page=first_page)

    async def fetch_first_page(self) -> list[JobPosting]:
        return (await self._page(0))[0]

    async def fetch_newest(self, first_page: list[JobPosting]) -> ScrapeResult:
        """Pages from the top until one holds a job already known; the next full check finds any missed."""
        jobs = {job.uid: job for job in first_page}
        page, offset, pages = first_page, len(first_page), 1
        while page and not any(job.uid in self.known for job in page) and pages < QUICK_MAX_PAGES:
            page = (await self._page(offset))[0]
            for job in page:
                jobs.setdefault(job.uid, job)
            offset, pages = offset + len(page), pages + 1
        return ScrapeResult(list(jobs.values()), complete=False, first_page=first_page)

    async def _headers(self) -> dict[str, str]:
        """The site's CSRF token and its cookie, sent explicitly so boards checked at once don't share one."""
        if self._session is None:
            resp = await self.http.request("GET", f"{SITE}/api/auth/csrf", headers={"Cookie": ""})
            try:
                token = resp.json()["csrfToken"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ScraperError("Dayforce gave no CSRF token") from exc
            cookie = "; ".join(f"{name}={value}" for name, value in resp.cookies.items())
            self._session = {"X-CSRF-TOKEN": str(token), "Cookie": cookie}
        return self._session

    async def _page(self, offset: int) -> tuple[list[JobPosting], int]:
        """One page of the listing from `offset`, and how many jobs the board has."""
        payload = {"clientNamespace": self.client, "jobBoardCode": self.board, "cultureCode": self.culture,
                   "paginationStart": offset}
        data = await self.http.post_json(f"{SITE}/api/geo/{self.client}/jobposting/search", payload,
                                         headers=await self._headers())
        postings = (data or {}).get("jobPostings") if isinstance(data, dict) else None
        if not isinstance(postings, list):
            raise ScraperError("unexpected Dayforce response shape")
        jobs = [self.parse_job(item) for item in postings if item.get("jobPostingId") and item.get("jobTitle")]
        return jobs, int(data.get("maxCount") or 0)

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id = str(item["jobPostingId"])
        places = [join_nonempty(p.get("cityName"), p.get("stateCode"),
                                COUNTRY_NAMES.get(p.get("isoCountryCode") or "", p.get("isoCountryCode")))
                  or p.get("formattedAddress") for p in item.get("postingLocations") or []]
        location = "; ".join(dict.fromkeys(p for p in places if p)) or None
        if item.get("hasVirtualLocation"):
            location = f"{location} (Remote)" if location else "Remote"
        return self.make_job(
            job_id=job_id,
            title=" ".join((item.get("jobTitle") or "").split()),
            url=f"{SITE}/{self.culture}/{self.client}/{self.board}/jobs/{job_id}",
            location=location,
            date_posted=parse_date(item.get("postingStartTimestampUTC")),
            extra={"description": item.get("jobDescription")},
        )
