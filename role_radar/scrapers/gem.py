"""Gem job boards (jobs.gem.com/{board}, e.g. Retool, Groq): Gem's public job board API.

  GET https://api.gem.com/job_board/v0/{board}/job_posts

answers every open job in one request, with its location and description (kept in memory,
for this check, only for the experience filter).
"""

from __future__ import annotations

from typing import Any

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date


class GemScraper(BaseScraper):
    name = "gem"
    domains = ("jobs.gem.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        board = self.options.get("board") or first_path_segment(self.company.url)
        if not board:
            raise ScraperError("could not determine the Gem board; set options.board")
        data = await self.http.get_json(f"https://api.gem.com/job_board/v0/{board}/job_posts")
        if not isinstance(data, list):
            raise ScraperError("unexpected Gem response shape")
        return ScrapeResult([self.parse_job(item) for item in data])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id, title = str(item.get("id") or ""), (item.get("title") or "").strip()
        if not job_id or not title:
            raise ScraperError("Gem job missing id or title")
        places = [o["location"]["name"] for o in item.get("offices") or [] if (o.get("location") or {}).get("name")]
        job = self.make_job(
            job_id=job_id, title=title, url=item.get("absolute_url") or "",
            location="; ".join(places) if len(places) > 1 else (item.get("location") or {}).get("name"),
            department=", ".join(d["name"] for d in item.get("departments") or [] if d.get("name")) or None,
            employment_type=(item.get("employment_type") or "").replace("_", "-") or None,
            date_posted=parse_date(item.get("first_published_at") or item.get("created_at")),
        )
        if item.get("content"):
            job.extra["description"] = item["content"]
        return job
