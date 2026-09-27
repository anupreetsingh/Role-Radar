"""Avature's public SearchJobs result cards (e.g. Bloomberg).

Career sites use different schemas; this adapter validates the supported
article--result layout and follows the site's actual Next links.
"""

from __future__ import annotations

import re

from role_radar.models import html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError
from role_radar.scrapers.generic import _PageParser
from role_radar.scrapers.html_pages import cards, next_link


class AvatureScraper(BaseScraper):
    name = "avature"
    domains = ("avature.net",)

    async def fetch_jobs(self) -> ScrapeResult:
        url = self.company.url
        jobs, visited = {}, set()
        for _ in range(int(self.options.get("max_pages", 150))):
            if url in visited:
                return ScrapeResult(list(jobs.values()), complete=False)
            visited.add(url)
            page = await self.http.get_text(url)
            rows = cards(page, "article", "article--result")
            if not rows:
                if not jobs and re.search(r"no (?:jobs|results|positions) (?:found|match|available)", html_to_text(page), re.I):
                    return ScrapeResult([])
                raise ScraperError("unsupported or missing Avature result cards")
            before = len(jobs)
            for row in rows:
                parser = _PageParser()
                parser.feed(row)
                link = next(((u, t) for u, t in parser.links if "/JobDetail/" in u), None)
                if not link:
                    raise ScraperError("Avature card missing job link")
                link_url, title = link
                job_id = link_url.rstrip("/").rsplit("/", 1)[-1]
                location = re.search(r'''<span[^>]*class=["'][^"']*list-item-location[^"']*["'][^>]*>(.*?)</span>''', row, re.I | re.S)
                jobs[job_id] = self.make_job(job_id=job_id, title=title, url=link_url, location=html_to_text(location[1]) if location else None)
            following = next_link(page, url)
            if not following:
                return ScrapeResult(list(jobs.values()))
            if len(jobs) == before:
                return ScrapeResult(list(jobs.values()), complete=False)
            url = following
        return ScrapeResult(list(jobs.values()), complete=False)
