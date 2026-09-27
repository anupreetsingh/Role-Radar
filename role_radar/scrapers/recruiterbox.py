"""Recruiterbox / Trakstar's public widget listing API, used by Wolfram.

options.widget_id comes from the employer's embedded widget. The API includes
descriptions and form fields, which are ignored. The employer's #op-ID fragment
opens the job in its own public widget without an application or account action.
"""

from __future__ import annotations

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty


class RecruiterboxScraper(BaseScraper):
    name = "recruiterbox"

    async def fetch_jobs(self) -> ScrapeResult:
        widget_id = str(self.options.get("widget_id", ""))
        if not widget_id.isdigit():
            raise ScraperError("Recruiterbox requires the public options.widget_id")
        data = await self.http.get_json(f"https://app.recruiterbox.com/widget/{widget_id}/openings/")
        if not isinstance(data, list):
            raise ScraperError("unexpected Recruiterbox listing shape")
        jobs = []
        for item in data:
            if not item.get("id") or not item.get("title"):
                raise ScraperError("Recruiterbox job missing id or title")
            place = item.get("location") or {}
            location = join_nonempty(place.get("city"), place.get("state"), place.get("country"))
            if item.get("allows_remote"):
                location = f"{location} (Remote)" if location else "Remote"
            job_id = str(item["id"])
            jobs.append(self.make_job(
                job_id=job_id, title=item["title"],
                url=f"{self.company.url.split('#')[0]}#op-{job_id}",
                location=location, department=item.get("team"), employment_type=item.get("position_type"),
            ))
        return ScrapeResult(jobs)
