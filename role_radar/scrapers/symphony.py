"""Symphony Talent career sites (Compass Group Canada's careers.compass-canada.com...), via the job search their
own pages call.

Careers URL: the site's job search page, e.g. https://careers.compass-canada.com/job-search-results/, with
options.organization: the number its page names in `cws_opts` ("org":"2052"). Without it, the page is read once
per check to find it.
  GET https://jobsapi-internal.m-cloud.io/api/job?Organization={org}&offset={n}&limit=100&sortfield=open_date&sortorder=descending
      100 jobs a page from the n-th (counting from 1), newest first, each with its places, its page on the career
      site and its whole description (the experience check needs nothing more)
Options: organization, max_jobs (default 2000).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from role_radar.models import JobPosting
from role_radar.scrapers.base import COUNTRY_NAMES, BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

API = "https://jobsapi-internal.m-cloud.io/api/job"
PAGE = 100
QUICK_MAX_PAGES = 2  # a quick check reads at most this many pages; a full check gets the rest
_ORG = re.compile(r'var cws_opts = \{[^}]*?"org":"(\d+)"')


def _place(city: Any, state: Any, country: Any) -> str | None:
    return join_nonempty(city, state, COUNTRY_NAMES.get(country or "", country))


class SymphonyScraper(BaseScraper):
    name = "symphony"  # set `ats: symphony`: the careers URL is the company's own site
    supports_quick = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._organization = str(self.options.get("organization") or "") or None

    async def organization(self) -> str:
        if self._organization is None:
            match = _ORG.search(await self.http.get_text(self.company.url))
            if not match:
                raise ScraperError("no Symphony Talent organization on the careers page; set options.organization")
            self._organization = match[1]
        return self._organization

    async def fetch_jobs(self) -> ScrapeResult:
        max_jobs = int(self.options.get("max_jobs", 2000))
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

    async def _page(self, offset: int) -> tuple[list[JobPosting], int]:
        """One page of the listing after the first `offset` jobs, and how many jobs the site has."""
        params = {"Organization": await self.organization(), "offset": offset + 1, "limit": PAGE,
                  "sortfield": "open_date", "sortorder": "descending"}
        data = await self.http.get_json(f"{API}?{urlencode(params)}")
        items = data.get("queryResult") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ScraperError("unexpected Symphony Talent response shape")
        jobs = [self.parse_job(item) for item in items if item.get("id") and item.get("title")]
        return jobs, int(data.get("totalHits") or 0)

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        places = [_place(item.get("primary_city"), item.get("primary_state"), item.get("primary_country"))]
        places += [_place(p.get("addtnl_city"), p.get("addtnl_state"), p.get("addtnl_country"))
                   for p in item.get("addtnl_locations") or [] if isinstance(p, dict)]
        return self.make_job(
            job_id=str(item["id"]),
            title=" ".join(str(item["title"]).split()),
            url=item.get("url") or self.company.url,
            location="; ".join(dict.fromkeys(p for p in places if p)) or None,
            employment_type=item.get("employment_type") or None,
            department=item.get("brand") or item.get("function") or None,
            date_posted=parse_date(item.get("open_date")),
            extra={"description": item.get("description")},
        )
