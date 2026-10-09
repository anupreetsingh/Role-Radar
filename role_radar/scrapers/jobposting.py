"""Career sites that list their jobs in a sitemap and mark each job page up with a schema.org
JobPosting (the data Google's job search reads), e.g. Dassault Systèmes, FedEx, Rapid7.

  options.sitemap    the sitemap (default {origin}/sitemap.xml); a sitemap index's children are
                     followed when their address mentions jobs or careers (up to 10)
  options.job_path   a regex a job page's path matches (default: a /job/ or /jobs/ segment
                     followed by more path)

A check reads the sitemap: every open job, by its link. A job already on record keeps its
stored title and location. A new one is listed under its link's words, which hold its title
(and sometimes its place), so the title keywords can still screen it: one that could match has
its page read for the JobPosting's title, places, date and description before it's matched.
Jobs listed at a company's first check are a baseline that isn't alerted, so their pages
aren't read. Checked hourly by default, like the other sitemap readers.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import unquote, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date
from role_radar.scrapers.radancy import _places, _posting

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_JOB_PATH = r"/jobs?/[^/?#]+"
# A path segment, or the end of one, that identifies the job: 550471, P25-354770-1, a UUID.
_ID = re.compile(r"(?:^|-)((?:[A-Z]\d+-)?\d{3,}(?:-\d+)*|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_GENERIC = {"job", "jobs", "career", "careers", "en", "us", "en-us", "position", "positions"}
MAX_SITEMAPS = 10


def link_job(path: str) -> tuple[str, str]:
    """A job link's ID and words: "/careers/jobs/spatial-services-software-architect-550471" →
    ("550471", "spatial services software architect")."""
    segments = [unquote(s) for s in path.split("/") if s and s.lower() not in _GENERIC]
    job_id = None
    for at in range(len(segments) - 1, -1, -1):
        found = _ID.search(segments[at])
        if found:
            job_id = found[1]
            segments[at] = segments[at][: found.start()]
            break
    words = " ".join(re.findall(r"[^\W_]+", " ".join(segments)))
    return job_id or path.rstrip("/"), words


class JobPostingScraper(BaseScraper):
    name = "jobposting"

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        if not parts.netloc:
            raise ScraperError(f"not a career site URL: {self.company.url!r}")
        sitemap = self.options.get("sitemap") or f"{parts.scheme or 'https'}://{parts.netloc}/sitemap.xml"
        job_path = re.compile(self.options.get("job_path") or _JOB_PATH, re.I)
        jobs: dict[str, JobPosting] = {}
        for loc in await self._links(sitemap):
            loc = unescape(loc)
            path = urlsplit(loc).path
            if not job_path.search(path):
                continue
            job_id, words = link_job(path)
            if job_id in jobs or not words:
                continue
            job = self.make_job(job_id=job_id, title=words, url=loc, extra={"from_link": True})
            known = self.known.get(job.uid)
            if known and known.title:
                job = self.make_job(job_id=job_id, title=known.title, url=loc, location=known.location)
            jobs[job_id] = job
        if not jobs:
            raise ScraperError("the sitemap lists no job pages")
        return ScrapeResult(list(jobs.values()))

    async def _links(self, url: str) -> list[str]:
        xml = await self.http.get_text(url)
        if "<sitemapindex" in xml:
            children = [u for u in _LOC.findall(xml) if re.search(r"job|career|position", u, re.I)]
            links: list[str] = []
            for child in children[:MAX_SITEMAPS]:
                links += _LOC.findall(await self.http.get_text(unescape(child)))
            return links
        if "<urlset" not in xml:
            raise ScraperError("the sitemap isn't a list of links")
        return _LOC.findall(xml)

    def missing_fields(self, job: JobPosting) -> set[str]:
        return {"location"} if job.extra.get("from_link") else set()

    def wants_details(self, job: JobPosting) -> bool:
        # Not at the first check, whose jobs are a baseline (self.known is empty then).
        return bool(job.extra.get("from_link") and self.known)

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        posting = _posting(await self.http.get_text(job.url))
        if not posting or not posting.get("title"):
            raise ScraperError(f"no JobPosting on the job page {job.url}")
        job.title = unescape(str(posting["title"])).strip()
        job.location = _places(posting) or job.location
        job.date_posted = job.date_posted or parse_date(posting.get("datePosted"))
        job.extra.update(from_link=False, description=posting.get("description"))
        return job

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        if job.extra.get("description"):
            return job.extra["description"]
        posting = _posting(await self.http.get_text(job.url))
        return posting.get("description") if posting else None
