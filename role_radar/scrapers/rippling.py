"""Rippling Recruiting public job boards (ats.rippling.com/{board}/jobs).

  GET https://ats.rippling.com/api/v2/board/{board}/jobs?page=N&pageSize=1000
  GET https://ats.rippling.com/api/v2/board/{board}/jobs/{id}   (one job's details)

The listing repeats a job once per location, so entries are merged by job ID. It
has no description, time type or posting date; the detail request has all three
and is made only for a job the filters or the experience check need it for.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date

API = "https://ats.rippling.com/api/v2/board"
PAGE_SIZE = 1000  # the most the API returns per page
_LOCALE = re.compile(r"^[a-z]{2}-(?:[A-Z]{2}|\d{3})$")  # "en-GB", "es-419"


def board_name(url: str) -> str | None:
    """The board in https://ats.rippling.com/[locale/]{board}/jobs[/...]."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments and _LOCALE.match(segments[0]):
        segments = segments[1:]
    return segments[0] if segments else None


def _location(item: dict[str, Any]) -> str | None:
    name = (item.get("name") or "").strip()
    country = (item.get("country") or "").strip()
    if country and country.lower() not in name.lower():
        name = f"{name}, {country}" if name else country
    if item.get("workplaceType") == "REMOTE" and name and "remote" not in name.lower():
        name += " (Remote)"
    return name or None


class RipplingScraper(BaseScraper):
    name = "rippling"
    domains = ("ats.rippling.com",)

    @property
    def board(self) -> str:
        board = self.options.get("board_name") or board_name(self.company.url)
        if not board:
            raise ScraperError("could not determine Rippling board name; set options.board_name")
        return board

    async def fetch_jobs(self) -> ScrapeResult:
        board = self.board
        max_pages = int(self.options.get("max_pages", 5))
        jobs: dict[str, JobPosting] = {}
        for page in range(max_pages):
            data = await self.http.get_json(f"{API}/{board}/jobs?page={page}&pageSize={PAGE_SIZE}")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ScraperError("unexpected Rippling response shape")
            for item in data["items"]:
                if item.get("id"):
                    self._add(jobs, item)
            if page + 1 >= int(data.get("totalPages") or 0):
                return ScrapeResult(list(jobs.values()))
        return ScrapeResult(list(jobs.values()), complete=False)

    def _add(self, jobs: dict[str, JobPosting], item: dict[str, Any]) -> None:
        locations = [loc for loc in map(_location, item.get("locations") or []) if loc]
        job = jobs.get(item["id"])
        if job is None:
            jobs[item["id"]] = self.parse_job(item, locations)
        elif locations:  # the same job listed under another location
            known = job.location.split("; ") if job.location else []
            job.location = "; ".join(dict.fromkeys([*known, *locations]))

    def parse_job(self, item: dict[str, Any], locations: list[str]) -> JobPosting:
        return self.make_job(
            job_id=str(item["id"]),
            title=(item.get("name") or "").strip(),
            url=item.get("url") or f"https://ats.rippling.com/{self.board}/jobs/{item['id']}",
            location="; ".join(dict.fromkeys(locations)) or None,
            department=(item.get("department") or {}).get("name"),
        )

    def missing_fields(self, job: JobPosting) -> set[str]:
        return {"employment_type"}  # only the job's own page has it

    async def _detail(self, job: JobPosting) -> None:
        data = await self.http.get_json(f"{API}/{self.board}/jobs/{job.job_id}")
        data = data if isinstance(data, dict) else {}
        description = data.get("description")
        if isinstance(description, dict):  # {"role": ..., "company": ...}; the company blurb can mislead the experience check
            description = description.get("role") or description.get("company")
        job.extra["description"] = description if isinstance(description, str) else None
        job.employment_type = (data.get("employmentType") or {}).get("id") or job.employment_type
        job.date_posted = parse_date(data.get("createdOn")) or job.date_posted

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        await self._detail(job)
        return job

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        if "description" not in job.extra:  # the detail wasn't fetched for the filter
            await self._detail(job)
        return job.extra["description"]
