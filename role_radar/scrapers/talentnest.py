"""TalentNest career sites ({company}.talentnest.com, e.g. Pomeroy Lodging's).

  GET https://{company}.talentnest.com/en?page={n}   twenty jobs a page: title, business unit and place
  GET https://{company}.talentnest.com/en/posting/{id}   a job's page, read only for the experience check

The site's /api is closed to programs by its robots.txt; its pages aren't.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlsplit

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

MAX_PAGES = 50
_ROW = re.compile(r"""<div class='job-row\b[^']*' data-job-row-url='/posting/(\d+)'(.*?)(?=<div class='job-row\b|$)""", re.S)
_TITLE = re.compile(r'<a class="job-link" href="[^"]*">(.*?)</a>', re.S)
_CELL = re.compile(r"""<div class='posting-cell-content (business-unit|location)'>\s*<div class='posting-heading'>[^<]*</div>([^<]*)""")
_NEXT = re.compile(r'<a class="page next_page" rel="next" href="[^"]*[?&]page=(\d+)"')
_DESCRIPTION = re.compile(r"<div class='field rich-text-doc'>(.*?)(?:<div class='field' style='text-align: center;'>|$)", re.S)


def _clean(text: str) -> str:
    """'chetwynd, British Columbia' → 'Chetwynd, British Columbia'."""
    return ", ".join(p[:1].upper() + p[1:] if p.islower() else p for p in " ".join(unescape(text).split()).split(", "))


class TalentNestScraper(BaseScraper):
    name = "talentnest"
    domains = ("talentnest.com",)

    @property
    def site(self) -> str:
        host = urlsplit(self.company.url).hostname
        if not host or host in ("talentnest.com", "www.talentnest.com"):
            raise ScraperError("use the company's own site, https://{company}.talentnest.com")
        return f"https://{host}"

    async def fetch_jobs(self) -> ScrapeResult:
        jobs: dict[str, JobPosting] = {}
        page_number: int | None = 1
        for _ in range(MAX_PAGES):
            page = await self.http.get_text(f"{self.site}/en?page={page_number}")
            for job_id, row in _ROW.findall(page):
                title = _TITLE.search(row)
                if title and job_id not in jobs:
                    cells = {kind: _clean(text) for kind, text in _CELL.findall(row)}
                    jobs[job_id] = self.make_job(
                        job_id=job_id, title=html_to_text(title[1]), url=f"{self.site}/en/posting/{job_id}",
                        location=cells.get("location") or None, department=cells.get("business-unit") or None,
                    )
            following = _NEXT.search(page)
            page_number = int(following[1]) if following and int(following[1]) > page_number else None
            if page_number is None:
                break
        else:
            return ScrapeResult(list(jobs.values()), complete=False)
        if not jobs and "job-row" in page:
            raise ScraperError("TalentNest job list found but no jobs could be read from it")
        return ScrapeResult(list(jobs.values()))

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        match = _DESCRIPTION.search(await self.http.get_text(job.url))
        return match[1] if match else None
