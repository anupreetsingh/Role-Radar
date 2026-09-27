"""BambooHR public careers API.

  GET https://{sub}.bamboohr.com/careers/list          → all openings

The per-job `/careers/{id}/detail` endpoint is never called: it only adds the
description, the posting date and a share URL, and the listing already has
title, location, department and employment type.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from models import JobPosting
from scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty

# BambooHR's locationType codes.
_WORKPLACE = {"1": "Remote", "2": "Hybrid"}


class BambooHRScraper(BaseScraper):
    name = "bamboohr"
    domains = ("bamboohr.com",)

    @property
    def base(self) -> str:
        sub = self.options.get("subdomain") or urlsplit(self.company.url).netloc.split(".")[0]
        return f"https://{sub}.bamboohr.com/careers"

    async def fetch_jobs(self) -> ScrapeResult:
        data = await self.http.get_json(f"{self.base}/list")
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise ScraperError("unexpected BambooHR response shape")
        return ScrapeResult([self.parse_listing(item) for item in data["result"] if item.get("id")])

    def parse_listing(self, item: dict[str, Any]) -> JobPosting:
        job_id = str(item["id"])
        return self.make_job(
            job_id=job_id,
            title=(item.get("jobOpeningName") or "").strip(),
            url=f"{self.base}/{job_id}",
            location=self._location(item),
            employment_type=item.get("employmentStatusLabel") or item.get("employmentType"),
            department=item.get("departmentLabel"),
        )

    @staticmethod
    def _location(item: dict[str, Any]) -> str | None:
        loc = item.get("location") or {}
        ats = item.get("atsLocation") or {}
        place = join_nonempty(
            loc.get("city") or ats.get("city"),
            loc.get("state") or ats.get("state") or ats.get("province"),
            ats.get("country"),
        )
        workplace = "Remote" if item.get("isRemote") else _WORKPLACE.get(str(item.get("locationType")))
        if workplace and place:
            return f"{place} ({workplace})"
        return place or workplace
