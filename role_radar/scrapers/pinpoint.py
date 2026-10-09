"""Pinpoint career sites ({company}.pinpointhq.com, or the company's own domain, e.g. Infor).

  GET {origin}/postings.json

answers every open posting in one request: title, link, place (city, province), employment
type, department and description (kept in memory, for this check, only for the experience
filter). A site that groups its jobs by a custom "Country" field (Infor) has that added to
the place.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty


class PinpointScraper(BaseScraper):
    name = "pinpoint"
    domains = ("pinpointhq.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        data = await self.http.get_json(f"{parts.scheme}://{parts.netloc}/postings.json")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ScraperError("unexpected Pinpoint response shape")
        return ScrapeResult([self.parse_job(item) for item in data["data"]])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id, title = str(item.get("id") or ""), (item.get("title") or "").strip()
        if not job_id or not title:
            raise ScraperError("Pinpoint posting missing id or title")
        job_info = item.get("job") or {}
        place = item.get("location") or {}
        country = next((v.get("name") for k, v in job_info.items()
                        if k.startswith("structure_custom_group") and isinstance(v, dict) and (v.get("title") or "").lower() == "country"), None)
        job = self.make_job(
            job_id=job_id, title=title, url=item.get("url") or "",
            location=join_nonempty(place.get("city") or place.get("name"), place.get("province"), country),
            department=(job_info.get("department") or {}).get("name"),
            employment_type=item.get("employment_type_text") or item.get("employment_type"),
        )
        description = "\n".join(item[k] for k in ("description", "key_responsibilities", "skills_knowledge_expertise") if item.get(k))
        if description:
            job.extra["description"] = description
        return job
