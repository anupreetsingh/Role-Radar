"""Recruitee career sites ({company}.recruitee.com, or a custom domain with `ats: recruitee`).

  GET https://{site}/api/offers/   every published offer with its places and its whole description
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date


class RecruiteeScraper(BaseScraper):
    name = "recruitee"
    domains = ("recruitee.com",)

    @property
    def site(self) -> str:
        parts = urlsplit(self.company.url)
        if not parts.hostname:
            raise ScraperError("could not determine the Recruitee site from the URL")
        return f"https://{parts.hostname}"

    async def fetch_jobs(self) -> ScrapeResult:
        data = await self.http.get_json(f"{self.site}/api/offers/")
        offers = data.get("offers") if isinstance(data, dict) else None
        if not isinstance(offers, list):
            raise ScraperError("unexpected Recruitee response shape")
        return ScrapeResult([self.parse_job(o) for o in offers if o.get("id") and o.get("title")])

    def parse_job(self, offer: dict[str, Any]) -> JobPosting:
        places = [join_nonempty(p.get("city"), p.get("state_code") or p.get("state"), p.get("country"))
                  for p in offer.get("locations") or []]
        places = places or [join_nonempty(offer.get("city"), offer.get("state_code"), offer.get("country"))]
        location = "; ".join(dict.fromkeys(p for p in places if p)) or None
        if offer.get("remote"):
            location = f"{location} (Remote)" if location else "Remote"
        return self.make_job(
            job_id=str(offer["id"]),
            title=" ".join(str(offer["title"]).split()),
            url=offer.get("careers_url") or f"{self.site}/o/{offer.get('slug')}",
            location=location,
            employment_type=offer.get("employment_type_code") or None,
            department=offer.get("department") or None,
            date_posted=parse_date(offer.get("published_at") or offer.get("created_at")),
            extra={"description": (offer.get("description") or "") + (offer.get("requirements") or "") or None},
        )
