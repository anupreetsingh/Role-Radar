"""COMSOL's public careers page groups job links beneath city/country headings."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from role_radar.models import html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError
from role_radar.scrapers.generic import _PageParser


class ComsolScraper(BaseScraper):
    name = "comsol"
    domains = ("comsol.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        page = await self.http.get_text(self.company.url)
        sections = re.split(r"<h3\b[^>]*>(.*?)</h3>", page, flags=re.I | re.S)
        jobs = {}
        for heading, body in zip(sections[1::2], sections[2::2]):
            parser = _PageParser()
            parser.feed(body)
            for href, title in parser.links:
                match = re.search(r"/company/careers/job/(\d+)", href)
                if match and title:
                    jobs[match[1]] = self.make_job(job_id=match[1], title=title, url=urljoin(self.company.url, href), location=html_to_text(heading))
        if not jobs:
            raise ScraperError("COMSOL location groups/job links missing")
        return ScrapeResult(list(jobs.values()), complete=False)
