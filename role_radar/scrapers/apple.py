"""jobs.apple.com search pages. They're server-rendered, with the results in the
page's hydration JSON (window.__staticRouterHydrationData).

Careers URL: https://jobs.apple.com/en-us/search?location=united-states-USA. The
URL's query string (location, search, team...) is kept; results are sorted newest
first, 20 a page, and a check reads options.max_pages pages (default 10). It's a
removal snapshot only when that covers every result. The experience filter reads a
new match's minimum qualifications from its details page (the same hydration JSON).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, join_nonempty, parse_date

_HYDRATION = re.compile(r'__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', re.S)


def search_data(html: str) -> dict[str, Any]:
    """The search route's loader data from a jobs.apple.com search page."""
    return _loader_data(html, "search")


def _loader_data(html: str, route: str) -> dict[str, Any]:
    m = _HYDRATION.search(html)
    if not m:
        raise ScraperError("jobs.apple.com page has no hydration data")
    try:
        data = json.loads(json.loads(f'"{m.group(1)}"'))  # a JSON document inside a JS string literal
        return data["loaderData"][route]
    except (ValueError, KeyError, TypeError) as exc:
        raise ScraperError(f"jobs.apple.com hydration data unreadable: {exc}") from None


class AppleScraper(BaseScraper):
    name = "apple"
    domains = ("jobs.apple.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        locale = first_path_segment(self.company.url) or "en-us"
        endpoint = f"{origin}/{locale}/search"
        query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("sort", "page")] + [("sort", "newest")]

        jobs = {}
        for page in range(1, int(self.options.get("max_pages", 10)) + 1):
            data = search_data(await self.http.get_text(endpoint, params=[*query, ("page", page)]))
            results, total = data.get("searchResults"), data.get("totalRecords")
            if not isinstance(results, list) or not isinstance(total, int):
                raise ScraperError("unexpected jobs.apple.com search data")
            for item in results:
                job_id, title = item.get("positionId") or item.get("id"), (item.get("postingTitle") or "").strip()
                if not job_id or not title:
                    continue
                places = [join_nonempty(p.get("city"), p.get("stateProvince"), p.get("countryName")) for p in item.get("locations") or []]
                slug = item.get("transformedPostingTitle") or ""
                jobs[str(job_id)] = self.make_job(
                    job_id=str(job_id), title=title,
                    url=f"{origin}/{locale}/details/{job_id}/{slug}".rstrip("/"),
                    location="; ".join(p for p in places if p) or None,
                    department=(item.get("team") or {}).get("teamName"),
                    date_posted=parse_date(item.get("postDateInGMT") or item.get("postingDate")),
                )  # fmt: skip
            if not results or len(jobs) >= total:
                return ScrapeResult(list(jobs.values()), complete=len(jobs) >= total)
        return ScrapeResult(list(jobs.values()), complete=False)

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        details = _loader_data(await self.http.get_text(job.url), "jobDetails")
        return (details.get("jobsData") or {}).get("minimumQualifications")
