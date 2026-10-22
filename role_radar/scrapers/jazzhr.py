"""JazzHR career pages ({company}.applytojob.com).

  GET https://{company}.applytojob.com/apply                every open job: its title, place and department
  GET https://{company}.applytojob.com/apply/{id}/{slug}    a job's page, read only for the experience check
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlsplit

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

_JOB = re.compile(r'<a\b[^>]*\bhref="(https?://[^"/]+/apply/([A-Za-z0-9]+)/[^"]*)"[^>]*>(.*?)</a>', re.S)
_PLACE = re.compile(r"""fa-map-marker['"]\s*>\s*</i>\s*([^<]+)""")
_TEAM = re.compile(r"""fa-sitemap['"]\s*>\s*</i>\s*([^<]+)""")
_DESCRIPTION = re.compile(r'<div[^>]*\bid="job-description"[^>]*>(.*?)(?:<div[^>]*\bid="apply|<form|$)', re.S)


class JazzHRScraper(BaseScraper):
    name = "jazzhr"
    domains = ("applytojob.com",)

    @property
    def site(self) -> str:
        host = urlsplit(self.company.url).hostname
        if not host:
            raise ScraperError("could not determine the JazzHR page from the URL")
        return f"https://{host}"

    async def fetch_jobs(self) -> ScrapeResult:
        page = await self.http.get_text(f"{self.site}/apply")
        jobs: dict[str, JobPosting] = {}
        found = list(_JOB.finditer(page))
        for i, match in enumerate(found):
            url, job_id, title = match[1], match[2], html_to_text(match[3])
            if not title or job_id in jobs:
                continue
            rest = page[match.end():found[i + 1].start() if i + 1 < len(found) else len(page)]
            place, team = _PLACE.search(rest), _TEAM.search(rest)
            jobs[job_id] = self.make_job(
                job_id=job_id, title=title, url=unescape(url),
                location=" ".join(unescape(place[1]).split()) if place else None,
                department=" ".join(unescape(team[1]).split()) if team else None,
            )
        if not jobs and "list-group-item" in page:
            raise ScraperError("JazzHR job list found but no jobs could be read from it")
        return ScrapeResult(list(jobs.values()))

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        match = _DESCRIPTION.search(await self.http.get_text(job.url))
        return match[1] if match else None
