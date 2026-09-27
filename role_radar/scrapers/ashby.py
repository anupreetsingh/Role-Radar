"""Ashby public Job Posting API.

  GET https://api.ashbyhq.com/posting-api/job-board/{board}

The API always includes descriptions and has no option to leave them out;
they're ignored.
"""

from __future__ import annotations

import re
from typing import Any

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date


def _humanize(value: str | None) -> str | None:
    # "FullTime" → "Full Time"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value) if value else None


class AshbyScraper(BaseScraper):
    name = "ashby"
    domains = ("ashbyhq.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        board = self.options.get("board_name") or first_path_segment(self.company.url)
        if not board:
            raise ScraperError("could not determine Ashby board name; set options.board_name")
        data = await self.http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ScraperError("unexpected Ashby response shape")
        return ScrapeResult([self.parse_job(j) for j in data["jobs"] if j.get("id") and j.get("isListed", True)])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        locations = [item.get("location")] + [s.get("location") for s in item.get("secondaryLocations") or []]
        location = "; ".join(dict.fromkeys(loc for loc in locations if loc)) or None
        if item.get("isRemote") and location and "remote" not in location.lower():
            location += " (Remote)"
        return self.make_job(
            job_id=str(item["id"]),
            title=(item.get("title") or "").strip(),
            url=item.get("jobUrl") or item.get("applyUrl") or "",
            location=location,
            employment_type=_humanize(item.get("employmentType")),
            department=item.get("department") or item.get("team"),
            date_posted=parse_date(item.get("publishedAt")),
        )
