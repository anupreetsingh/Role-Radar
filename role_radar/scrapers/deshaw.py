"""The D. E. Shaw group's career sites (www.deshaw.com, www.deshawindia.com).

Each careers page (deshaw.com/careers/choose-your-path, deshawindia.com/careers) is a Next.js
page whose __NEXT_DATA__ holds every open job: `regularJobs` and `internships` (its
`internalJobs` are for current staff and are skipped). A job names its offices ("New York",
"Gurugram"), which are given their state and country so location filters can place them; its
page is {origin}/careers/{jobUrl}. The listing includes each job's description, so the
experience filter needs no extra request.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
OFFICES = {
    "New York": "New York, NY, United States",
    "Denver": "Denver, CO, United States",
    "London": "London, United Kingdom",
    "Singapore": "Singapore",
    "Hong Kong": "Hong Kong",
    "Hyderabad": "Hyderabad, India",
    "Bengaluru": "Bengaluru, India",
    "Gurugram": "Gurugram, India",
    "Off-site": "Remote",
}
LISTS = ("regularJobs", "internships")


class DEShawScraper(BaseScraper):
    name = "deshaw"
    domains = ("deshaw.com", "deshawindia.com")

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        found = _NEXT_DATA.search(await self.http.get_text(self.company.url))
        if not found:
            raise ScraperError("D. E. Shaw careers page has no __NEXT_DATA__")
        props = ((json.loads(found[1]).get("props") or {}).get("pageProps")) or {}
        if not any(isinstance(props.get(key), list) for key in LISTS):
            raise ScraperError("D. E. Shaw careers page lists no jobs")
        jobs: dict[str, JobPosting] = {}
        for key in LISTS:
            for item in props.get(key) or []:
                job = self.parse_job(item, origin)
                jobs[job.job_id] = job
        return ScrapeResult(list(jobs.values()))

    def parse_job(self, item: dict[str, Any], origin: str) -> JobPosting:
        data = item.get("data") or {}
        job_id = str(item.get("id") or data.get("id") or "")
        title = (item.get("displayName") or data.get("displayName") or "").strip()
        slug = data.get("jobUrl")
        if not job_id or not title or not slug:
            raise ScraperError("D. E. Shaw job missing id, title or link")
        offices = [o.get("name") for o in item.get("office") or [] if o.get("name")]
        about = data.get("jobDescription") or {}
        job = self.make_job(
            job_id=job_id, title=title, url=f"{origin}/careers/{slug}",
            location="; ".join(OFFICES.get(o, o) for o in offices) or None,
            department=", ".join(c for c in item.get("category") or [] if c) or None,
            employment_type=(data.get("jobMetadata") or {}).get("workStatus") or data.get("status"),
        )
        description = " ".join(filter(None, (about.get("websiteDescription"), about.get("responsibilitiesHtml"),
                                             about.get("peopleWeAreLookingForStr"))))
        if description:
            job.extra["description"] = description
        return job
