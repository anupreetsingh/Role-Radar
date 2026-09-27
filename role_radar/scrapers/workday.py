"""Workday public job-search JSON API (the one the careers site itself calls).

Careers URL:  https://{tenant}.wd5.myworkdayjobs.com/[en-US/]{site}
  POST https://{host}/wday/cxs/{tenant}/{site}/jobs      → paginated listing (max 20/page)
  GET  https://{host}/wday/cxs/{tenant}/{site}{path}     → job detail

The listing has no employment type and shows "3 Locations" instead of the
place names, so a job's detail is fetched only when the company's filter
needs one of those: an employment-type rule, or a location rule for a job
listed under "N Locations". The detail's description is ignored.

Large employers list thousands of jobs, so use options.search_text (and
optionally options.applied_facets) to narrow the query, and options.max_jobs
to cap pages.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date

PAGE_SIZE = 20
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_MULTI_LOCATION = re.compile(r"^\d+\s+locations?$", re.I)


class WorkdayScraper(BaseScraper):
    name = "workday"
    domains = ("myworkdayjobs.com", "myworkdaysite.com")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        parts = urlsplit(self.company.url)
        self.host = parts.netloc
        segments = [s for s in parts.path.split("/") if s and not _LOCALE.match(s)]
        recruiting = len(segments) >= 3 and segments[0] == "recruiting"
        self.tenant = self.options.get("tenant") or (segments[1] if recruiting else self.host.split(".")[0])
        self.site = self.options.get("site") or (segments[2] if recruiting else (segments[0] if segments else None))
        if not self.site:
            raise ScraperError("could not determine Workday site from URL; set options.site")
        self.api = f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"
        prefix = f"/recruiting/{self.tenant}" if recruiting else ""
        self.career_base = f"https://{self.host}{prefix}/{self.site}"

    async def fetch_jobs(self) -> ScrapeResult:
        max_jobs = int(self.options.get("max_jobs", 500))
        payload: dict[str, Any] = {
            "appliedFacets": self.options.get("applied_facets") or {},
            "limit": PAGE_SIZE,
            "offset": 0,
            "searchText": self.options.get("search_text", ""),
        }
        jobs: dict[str, JobPosting] = {}
        total: int | None = None
        while payload["offset"] < max_jobs:
            data = await self.http.post_json(f"{self.api}/jobs", payload)
            postings = (data or {}).get("jobPostings")
            if not isinstance(postings, list):
                raise ScraperError("unexpected Workday response shape")
            # Workday only reports a reliable total on the first page.
            if total is None:
                total = int(data.get("total") or 0)
            for item in postings:
                if item.get("externalPath"):
                    job = self.parse_listing(item)
                    jobs.setdefault(job.uid, job)  # pages can overlap while jobs churn
            payload["offset"] += PAGE_SIZE
            if not postings or payload["offset"] >= total:
                return ScrapeResult(list(jobs.values()))
        return ScrapeResult(list(jobs.values()), complete=False)

    def parse_listing(self, item: dict[str, Any]) -> JobPosting:
        path = item["externalPath"]
        return self.make_job(
            job_id=self._job_id(path, item.get("bulletFields") or []),
            title=(item.get("title") or "").strip(),
            url=f"{self.career_base}{path}",
            location=item.get("locationsText"),
            date_posted=parse_date(item.get("postedOn")),
            extra={"path": path},
        )

    def missing_fields(self, job: JobPosting) -> set[str]:
        missing = {"employment_type"}  # the listing never includes the time type
        if not job.location or _MULTI_LOCATION.match(job.location.strip()):
            missing.add("location")
        return missing

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        data = await self.http.get_json(f"{self.api}{job.extra['path']}")
        info = (data or {}).get("jobPostingInfo") or {}
        job.employment_type = info.get("timeType") or job.employment_type
        job.date_posted = parse_date(info.get("startDate")) or job.date_posted
        if info.get("location") and "location" in self.missing_fields(job):
            # Listing shows "3 Locations" (or nothing); the detail has the real names.
            job.location = "; ".join([info["location"], *(info.get("additionalLocations") or [])])
        return job

    @staticmethod
    def _job_id(path: str, bullets: list[str]) -> str:
        # Paths end in "..._JR2015623"; that requisition suffix survives title edits.
        tail = path.rsplit("_", 1)[-1] if "_" in path else ""
        if tail and re.fullmatch(r"[A-Za-z]*-?\d[\w-]*", tail):
            return tail
        return bullets[0] if bullets else path
