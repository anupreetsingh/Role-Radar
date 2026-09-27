"""Lever Postings API (public, no auth).

  GET https://api.lever.co/v0/postings/{company}?mode=json&skip=N&limit=N
  (EU-hosted boards use api.eu.lever.co)

The API always includes descriptions and has no option to leave them out;
they're ignored.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date

PAGE_SIZE = 100


class LeverScraper(BaseScraper):
    name = "lever"
    domains = ("lever.co",)

    async def fetch_jobs(self) -> ScrapeResult:
        slug = self.options.get("company_slug") or first_path_segment(self.company.url)
        if not slug:
            raise ScraperError("could not determine Lever company slug; set options.company_slug")
        api = "api.eu.lever.co" if ".eu." in urlsplit(self.company.url).netloc else "api.lever.co"
        max_pages = int(self.options.get("max_pages", 20))

        jobs: list[JobPosting] = []
        for page in range(max_pages):
            url = f"https://{api}/v0/postings/{slug}?mode=json&skip={page * PAGE_SIZE}&limit={PAGE_SIZE}"
            data = await self.http.get_json(url)
            if not isinstance(data, list):
                raise ScraperError("unexpected Lever response shape")
            jobs.extend(self.parse_job(item) for item in data if item.get("id"))
            if len(data) < PAGE_SIZE:
                return ScrapeResult(jobs)
        return ScrapeResult(jobs, complete=False)

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        cats = item.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        return self.make_job(
            job_id=str(item["id"]),
            title=(item.get("text") or "").strip(),
            url=item.get("hostedUrl") or item.get("applyUrl") or "",
            location="; ".join(locations) or None,
            employment_type=cats.get("commitment"),
            department=cats.get("department") or cats.get("team"),
            date_posted=parse_date(item.get("createdAt")),
        )
