"""Comeet careers pages (www.comeet.com/jobs/{company}/{uid}, e.g. Cyera, Aqua Security, eToro).

The hosted careers page sets `COMPANY_POSITIONS_DATA = [...]`: every open position with its
department, employment type, place (city, state, country code, remote or not) and, on most
sites, its description, so one request reads a company. Internal positions are skipped. A
position listed without its description has it read from Comeet's page for it, whose
`POSITION_DATA` holds it, when the experience filter needs it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date


def _assigned(page: str, name: str) -> Any:
    """The JSON value a page's script assigns to `name` ("COMPANY_POSITIONS_DATA = [...]"), or None."""
    found = re.search(rf"\b{name}\s*=\s*(?=[\[{{])", page)
    if not found:
        return None
    try:
        return json.JSONDecoder().raw_decode(page, found.end())[0]
    except ValueError:
        return None


class ComeetScraper(BaseScraper):
    name = "comeet"
    domains = ("comeet.com", "comeet.co")

    async def fetch_jobs(self) -> ScrapeResult:
        positions = _assigned(await self.http.get_text(self.company.url), "COMPANY_POSITIONS_DATA")
        if not isinstance(positions, list):
            raise ScraperError("Comeet page has no COMPANY_POSITIONS_DATA")
        return ScrapeResult([self.parse_job(item) for item in positions if not item.get("is_internal")])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id, title = str(item.get("uid") or ""), (item.get("name") or "").strip()
        if not job_id or not title:
            raise ScraperError("Comeet position missing uid or name")
        place = item.get("location") or {}
        location = join_nonempty(place.get("city"), place.get("state"), place.get("country")) or place.get("name")
        if location and place.get("is_remote"):
            location = f"{location} (Remote)"
        job = self.make_job(
            job_id=job_id, title=title,
            url=item.get("url_active_page") or item.get("url_comeet_hosted_page") or "",
            location=location,
            department=item.get("department") or None,
            employment_type=item.get("employment_type") or None,
            date_posted=parse_date(item.get("time_updated")),
        )
        description = _details(item)
        if description:
            job.extra["description"] = description
        elif item.get("url_comeet_hosted_page"):
            job.extra["comeet_page"] = item["url_comeet_hosted_page"]
        return job

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        if job.extra.get("description") or not job.extra.get("comeet_page"):
            return job.extra.get("description")
        position = _assigned(await self.http.get_text(job.extra["comeet_page"]), "POSITION_DATA")
        return _details(position) if isinstance(position, dict) else None


def _details(position: dict[str, Any]) -> str | None:
    """A position's description sections ("Description", "Requirements"...) as one text."""
    details = (position.get("custom_fields") or {}).get("details") or position.get("details") or []
    return "\n".join(d["value"] for d in details if isinstance(d, dict) and d.get("value")) or None
