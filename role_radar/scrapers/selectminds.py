"""Oracle SelectMinds (Taleo) career sites' landing pages, e.g. Alberta Health Services'
careers.albertahealthservices.ca/landingpages/nutrition-food-opportunities-at-alberta-health-services-51.

  GET {landing page}   its newest jobs, about ten: title, place, requisition number and post date

The site's own search (/jobs/search/...), which pages through the rest, answers 403 to programs, so a check
sees only the newest page: enough to catch new jobs between checks, never a full listing (`complete` is
False, so jobs dropping off the page aren't counted as removed).
"""

from __future__ import annotations

import re
from html import unescape

from role_radar.models import html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

_ROW = re.compile(r'<div id="job_list_(\d+)" class="job_list_row\b(.*?)(?=<div id="job_list_\d+" class="job_list_row\b|$)', re.S)
_LINK = re.compile(r'<a href="([^"]+)" class="job_link\b[^"]*">(.*?)</a>', re.S)
_PLACE = re.compile(r'<p class="jlr_location">.*?</span>([^<]+)</a>', re.S)


class SelectMindsScraper(BaseScraper):
    name = "selectminds"
    domains = ("selectminds.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        page = await self.http.get_text(self.company.url)
        jobs = []
        for job_id, row in _ROW.findall(page):
            link, place = _LINK.search(row), _PLACE.search(row)
            if not link:
                continue
            jobs.append(self.make_job(
                job_id=job_id, title=html_to_text(link[2]), url=unescape(link[1]),
                location=" ".join(unescape(place[1]).split()) if place else None,
            ))
        if not jobs and "job_list_row" in page:
            raise ScraperError("SelectMinds job list found but no jobs could be read from it")
        return ScrapeResult(jobs, complete=False)
