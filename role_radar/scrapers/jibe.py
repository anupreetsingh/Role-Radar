"""iCIMS Jibe career-site public /api/jobs endpoint (e.g. Garmin).

Select ats: jibe for branded domains. Pages include descriptions; only listing
metadata is retained. Follow the reported total, guarding against repeated pages.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date


class JibeScraper(BaseScraper):
    name = "jibe"

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        endpoint = self.options.get("api_url", origin + "/api/jobs")
        jobs = {}
        for page in range(1, int(self.options.get("max_pages", 100)) + 1):
            data = await self.http.get_json(endpoint, params={"page": page, "limit": 100})
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ScraperError("unexpected Jibe response shape")
            total = data.get("totalCount", data.get("count"))
            if not isinstance(total, int) or total < 0:
                raise ScraperError("Jibe listing has no valid total")
            before = len(jobs)
            for entry in data["jobs"]:
                item = entry.get("data") or {}
                job_id = str(item.get("slug") or item.get("req_id") or "")
                if not job_id or not item.get("title"):
                    raise ScraperError("Jibe job missing id or title")
                jobs[job_id] = self.make_job(
                    job_id=job_id, title=item["title"],
                    url=(item.get("meta_data") or {}).get("canonical_url") or f"{origin}/jobs/{job_id}",
                    location=join_nonempty(item.get("city"), item.get("state"), item.get("country")) or item.get("full_location"),
                    department=", ".join(c["name"] for c in item.get("categories", []) if c.get("name")) or None,
                    date_posted=parse_date(item.get("posted_date")),
                )
            if len(jobs) >= total:
                return ScrapeResult(list(jobs.values()))
            if len(jobs) == before:
                return ScrapeResult(list(jobs.values()), complete=False)
        return ScrapeResult(list(jobs.values()), complete=False)
