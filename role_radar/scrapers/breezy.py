"""Breezy HR career portals ({company}.breezy.hr).

  GET https://{company}.breezy.hr/json      every published position with its place, in one response
  GET https://{company}.breezy.hr/p/{id}    a position's page, read only for the experience check
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

_DESCRIPTION = re.compile(r'<div[^>]*class="[^"]*\bdescription\b[^"]*"[^>]*>(.*?)(?:<div[^>]*class="[^"]*\bapply|</main>|$)', re.S)


class BreezyScraper(BaseScraper):
    name = "breezy"
    domains = ("breezy.hr",)

    @property
    def portal(self) -> str:
        """The portal's address: https://{company}.breezy.hr, or the custom domain it's on."""
        parts = urlsplit(self.company.url)
        if not parts.hostname:
            raise ScraperError("could not determine the Breezy portal from the URL")
        return f"https://{parts.hostname}"

    async def fetch_jobs(self) -> ScrapeResult:
        data = await self.http.get_json(f"{self.portal}/json")
        if not isinstance(data, list):
            raise ScraperError("unexpected Breezy response shape")
        return ScrapeResult([self.parse_job(item) for item in data if item.get("id") and item.get("name")])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        place = item.get("location") or {}
        location = join_nonempty(place.get("city"), (place.get("state") or {}).get("name"),
                                 (place.get("country") or {}).get("name")) or place.get("name")
        if place.get("is_remote"):
            location = f"{location} (Remote)" if location else "Remote"
        return self.make_job(
            job_id=str(item["id"]),
            title=" ".join(str(item["name"]).split()),
            url=item.get("url") or f"{self.portal}/p/{item.get('friendly_id') or item['id']}",
            location=location,
            employment_type=(item.get("type") or {}).get("name"),
            department=item.get("department") or None,
            date_posted=parse_date(item.get("published_date")),
        )

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        match = _DESCRIPTION.search(await self.http.get_text(job.url))
        return match[1] if match else None
