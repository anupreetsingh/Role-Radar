"""Google Careers results pages. They're server-rendered, with the jobs in the
page's `ds:1` AF_initDataCallback data.

Careers URL: https://www.google.com/about/careers/applications/jobs/results/?location=United%20States
Google's robots.txt allows a search's first page but not `page=`, so a check reads
only the first page (20 jobs, newest first with sort_by=date) of each query in
options.queries: extra filters added to the URL's own, by default one query for
early-career and one for mid-level roles. It's never a removal snapshot.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

RESULTS_PATH = "/about/careers/applications/jobs/results/"
DEFAULT_QUERIES = ("target_level=EARLY", "target_level=MID")
_DS1 = re.compile(r"AF_initDataCallback\(\{key: 'ds:1',.*?data:(.*?), sideChannel: \{\}\}\);", re.S)


def results_data(html: str) -> list[list[Any]]:
    """The job rows from a results page: [id, title, apply URL, ..., locations (9), ..., created (12)...]."""
    m = _DS1.search(html)
    if not m:
        raise ScraperError("Google Careers page has no ds:1 data")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise ScraperError(f"Google Careers ds:1 data unreadable: {exc}") from None
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise ScraperError("unexpected Google Careers data shape")
    return data[0]


class GoogleScraper(BaseScraper):
    name = "google"

    @classmethod
    def handles_url(cls, url: str) -> bool:
        parts = urlsplit(url)
        return parts.netloc.lower() in ("www.google.com", "google.com") and parts.path.startswith("/about/careers/")

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        endpoint = f"{parts.scheme}://{parts.netloc}{RESULTS_PATH}"
        base = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("page", "sort_by")] + [("sort_by", "date")]
        jobs = {}
        for query in self.options.get("queries") or DEFAULT_QUERIES:
            html = await self.http.get_text(endpoint, params=[*base, *parse_qsl(query)])
            for row in results_data(html):
                job_id, title = row[0] if row else None, row[1] if len(row) > 1 else None
                if not job_id or not title:
                    continue
                places = [p[0] for p in (row[9] if len(row) > 9 and isinstance(row[9], list) else []) if p and p[0]]
                created = row[12][0] if len(row) > 12 and isinstance(row[12], list) and row[12] else None
                jobs[str(job_id)] = self.make_job(
                    job_id=str(job_id), title=title.strip(), url=f"{endpoint}{job_id}",
                    location="; ".join(places) or None,
                    date_posted=datetime.fromtimestamp(created, tz=timezone.utc).date() if isinstance(created, int) else None,
                )  # fmt: skip
        return ScrapeResult(list(jobs.values()), complete=False)
