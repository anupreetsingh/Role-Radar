"""Radancy (TalentBrew) career sites on a company's own domain, e.g. jobs.intuit.com,
careers.unitedhealthgroup.com, careers.synopsys.com.

Their robots.txt closes the job search (/search-jobs/), but each site's sitemap lists every
open job's page, /job/{city}/{title}/{org}/{id} (some sites put a language first: /en/job/...),
and a job's page carries a schema.org JobPosting (title, places, posting date, description).

A check reads the sitemap, the whole listing in one request. A job already on record keeps
its stored title and location. A new one is listed under its link's words ("principal
product marketing manager accountant gtm", in "mountain view"), which hold the whole title,
so the title keywords can still screen it: one that could match has its own page read for
the real title and places before it's matched (fetch_details). Jobs listed at a company's
first check are a baseline that isn't alerted, so their pages aren't read. Because the
sitemap can be a few MB (UnitedHealth Group's, about 5,600 jobs), these sites are checked
hourly unless check_interval_by_ats.radancy says otherwise.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import unquote, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_JOB_LINK = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/job/([^/]+)/([^/]+)/(\d+)/(\d+)/?$", re.I)
_LD = re.compile(r'<script\b[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _words(slug: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unquote(slug)))


def _posting(page: str) -> dict[str, Any] | None:
    """The page's schema.org JobPosting, or None."""
    for block in _LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _places(posting: dict[str, Any]) -> str | None:
    places = posting.get("jobLocation") or []
    out = []
    for place in places if isinstance(places, list) else [places]:
        address = (place or {}).get("address") or {}
        country = address.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        found = join_nonempty(address.get("addressLocality"), address.get("addressRegion"), country)
        if found and found not in out:
            out.append(found)
    return "; ".join(out) or None


class RadancyScraper(BaseScraper):
    name = "radancy"

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        if not parts.netloc:
            raise ScraperError(f"not a career site URL: {self.company.url!r}")
        sitemap = self.options.get("sitemap") or f"{parts.scheme or 'https'}://{parts.netloc}/sitemap.xml"
        xml = await self.http.get_text(sitemap)
        if "<urlset" not in xml:
            raise ScraperError("the career site's sitemap.xml isn't a list of links")
        jobs: dict[str, JobPosting] = {}
        for loc in _LOC.findall(xml):
            loc = unescape(loc)
            link = _JOB_LINK.match(urlsplit(loc).path)
            if not link or link[4] in jobs:
                continue
            job_id = link[4]
            job = self.make_job(job_id=job_id, title=_words(link[2]), url=loc, location=_words(link[1]), extra={"from_link": True})
            known = self.known.get(job.uid)
            if known and known.title:
                job = self.make_job(job_id=job_id, title=known.title, url=loc, location=known.location)
            jobs[job_id] = job
        return ScrapeResult(list(jobs.values()))

    def missing_fields(self, job: JobPosting) -> set[str]:
        return {"location"} if job.extra.get("from_link") else set()

    def wants_details(self, job: JobPosting) -> bool:
        # A job known only by its link: its page has the real title and places. Not at the
        # first check, whose jobs are a baseline (self.known is empty then).
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
