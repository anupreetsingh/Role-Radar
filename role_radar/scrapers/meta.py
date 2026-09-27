"""Meta careers (metacareers.com), via its job sitemap plus each job's own page.

The search page is built in the browser, but /jobsearch/sitemap.xml lists every
open job. A job already on record reuses its stored title and location; a new one
costs a fetch of its page for the schema.org JobPosting data (title, location,
date posted), at most options.new_per_check (default 40) a check. Until every job
in the sitemap has been read, the listing isn't a removal snapshot.

The first checks work through the whole sitemap (~1,000 jobs), so pair this with
the company's max_alert_age_days: older jobs read late are recorded without alerting.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

from role_radar.http_client import FetchError
from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date
from role_radar.scrapers.generic import _employment_type, _ld_location, _PageParser, iter_job_postings

log = logging.getLogger(__name__)

_JOB_ID = re.compile(r"<loc>[^<]*/(?:job_details|jobs)/(\d+)/?</loc>")


class MetaScraper(BaseScraper):
    name = "meta"
    domains = ("metacareers.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        ids = list(dict.fromkeys(_JOB_ID.findall(await self.http.get_text(f"{origin}/jobsearch/sitemap.xml"))))
        if not ids:
            raise ScraperError("Meta job sitemap lists no jobs")
        budget = int(self.options.get("new_per_check", 40))
        jobs, unread = [], 0
        for job_id in ids:
            url = f"{origin}/profile/job_details/{job_id}/"
            known = self.known.get(self.make_job(job_id=job_id, title="", url=url).uid)
            if known and known.title:
                jobs.append(self.make_job(job_id=job_id, title=known.title, url=url, location=known.location))
                continue
            if budget <= 0:
                unread += 1
                continue
            budget -= 1
            try:
                job = self._from_page(await self.http.get_text(url), job_id, url)
            except (FetchError, ScraperError) as exc:
                log.warning("[%s] job %s unreadable: %s", self.company.name, job_id, exc)
                job = None
            if job:
                jobs.append(job)
            else:
                unread += 1
        if unread:
            log.info("[%s] %d of %d jobs still to read", self.company.name, unread, len(ids))
        return ScrapeResult(jobs, complete=unread == 0)

    def _from_page(self, page: str, job_id: str, url: str) -> JobPosting | None:
        parser = _PageParser()
        parser.feed(page)
        for block in parser.json_ld:
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for posting in iter_job_postings(data):
                title = html_to_text(posting.get("title") or posting.get("name"))
                if title:
                    return self.make_job(
                        job_id=job_id, title=title, url=url, location=_ld_location(posting),
                        employment_type=_employment_type(posting), date_posted=parse_date(posting.get("datePosted")),
                    )  # fmt: skip
        return None
