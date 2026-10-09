"""Jobvite career sites (jobs.jobvite.com/{company}, e.g. Nutanix).

  GET https://jobs.jobvite.com/{company}/jobs

is one table of every open job: its name, linking to /{company}/job/{id}, and its place.
The experience filter reads a new match's description from its job page.
"""

from __future__ import annotations

import re

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.I | re.S)
_NAME = re.compile(r'<td[^>]*jv-job-list-name[^>]*>\s*<a[^>]*href="(/[^"]+/job/([\w-]+))"[^>]*>(.*?)</a>', re.I | re.S)
_PLACE = re.compile(r'<td[^>]*jv-job-list-location[^>]*>(.*?)</td\s*>', re.I | re.S)
# The description runs from its div to the page's bottom section (it holds divs of its own).
_DESCRIPTION = re.compile(r'<div[^>]*class="[^"]*jv-job-detail-description[^>]*>(.*?)(?:<div[^>]*class="[^"]*jv-job-detail-bottom|</body)', re.I | re.S)


class JobviteScraper(BaseScraper):
    name = "jobvite"
    domains = ("jobs.jobvite.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        company = self.options.get("company") or first_path_segment(self.company.url)
        if not company:
            raise ScraperError("could not determine the Jobvite company; set options.company")
        page = await self.http.get_text(f"https://jobs.jobvite.com/{company}/jobs")
        jobs = {}
        for row in _ROW.findall(page):
            name = _NAME.search(row)
            if not name:
                continue
            place = _PLACE.search(row)
            jobs[name[2]] = self.make_job(job_id=name[2], title=" ".join(html_to_text(name[3]).split()), url=f"https://jobs.jobvite.com{name[1]}",
                                          location=" ".join(html_to_text(place[1]).split()) if place else None)
        if not jobs and "jv-job-list" not in page:
            raise ScraperError("Jobvite page has no job list")
        return ScrapeResult(list(jobs.values()))

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        found = _DESCRIPTION.search(await self.http.get_text(job.url))
        return found[1] if found else None
