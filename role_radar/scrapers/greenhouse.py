"""Greenhouse Job Board API (public, no auth).

  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Without `?content=true` the response has no descriptions, departments or
offices, which makes it roughly 10x smaller. Title, location, URL, metadata
and first_published are still included. The experience filter reads a new
match's description from its own endpoint:
  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date

# A location that names only a way of working: "Hybrid", "Distributed; Hybrid", "In-Office".
_WORK_MODE = re.compile(r"^(?:hybrid|distributed|in-office|in office|on-?site)(?:\s*[;,/]\s*(?:hybrid|distributed|in-office|in office|on-?site))*$", re.I)


class GreenhouseScraper(BaseScraper):
    name = "greenhouse"
    domains = ("greenhouse.io",)

    @property
    def board_token(self) -> str:
        if self.options.get("board_token"):
            return self.options["board_token"]
        parts = urlsplit(self.company.url)
        # Embed URLs look like boards.greenhouse.io/embed/job_board?for=acme
        token = parse_qs(parts.query).get("for", [None])[0] or first_path_segment(self.company.url)
        if not token or token == "embed":
            raise ScraperError("could not determine Greenhouse board token; set options.board_token")
        return token

    async def fetch_jobs(self) -> ScrapeResult:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs"
        data = await self.http.get_json(url)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ScraperError("unexpected Greenhouse response shape")
        return ScrapeResult([self.parse_job(item) for item in data["jobs"] if item.get("id")])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        return self.make_job(
            job_id=str(item["id"]),
            title=(item.get("title") or "").strip(),
            url=item.get("absolute_url") or "",
            location=self._location(item),
            employment_type=self._employment_type(item.get("metadata") or []),
            date_posted=parse_date(item.get("first_published") or item.get("updated_at")),
        )

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        data = await self.http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs/{job.job_id}")
        return (data or {}).get("content")

    @staticmethod
    def _location(item: dict[str, Any]) -> str | None:
        """The job's location; when that only says how people work (Cloudflare: "Hybrid"), the places
        in the job's metadata location field ("Job Posting Location": ["Austin, US"]), with the way of working."""
        name = (item.get("location") or {}).get("name")
        if not name or not _WORK_MODE.match(name.strip()):
            return name
        for entry in item.get("metadata") or []:
            if "location" in (entry.get("name") or "").lower():
                value = entry.get("value")
                places = "; ".join(str(v) for v in value if v) if isinstance(value, list) else str(value or "")
                if places:
                    return f"{places} ({name})"
        return name

    @staticmethod
    def _employment_type(metadata: list[dict[str, Any]]) -> str | None:
        # Employment type isn't a standard field; many boards expose it as custom metadata.
        for entry in metadata:
            name = (entry.get("name") or "").lower()
            if any(k in name for k in ("employment type", "time type", "job type", "commitment")):
                value = entry.get("value")
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value if v)
                return str(value) if value else None
        return None
