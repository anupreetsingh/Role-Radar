"""Phenom career sites (e.g. Chewy, OpenText, United Airlines): the site's own job feed.

A Phenom site's search page (careers.chewy.com/us/en/search-results) fills itself from

  POST {origin}/widgets  {"ddoKey": "refineSearch", "from": N, "size": 100, "lang": "en_us", "country": "us", ...}

which answers up to 100 jobs a request, with the total. The language and country come from
the URL's path (/us/en/ is en_us and us, /global/en/ is en_global and global), or from
options.lang and options.country. options.selected_fields narrows the search the way the
site's own filters do ({"country": ["United States of America"]}), options.keywords searches,
and options.max_jobs caps a big site; a capped read is never a removal snapshot. The feed's
"Most recent" order isn't reliably newest first, so every check reads the whole (narrowed)
list. The experience filter reads a new match's description from its job page
({origin}/us/en/job/{jobId}), whose phApp.ddo holds it.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

PAGE_SIZE = 100
_DDO = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", re.S)


class PhenomScraper(BaseScraper):
    name = "phenom"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        parts = urlsplit(self.company.url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        segments = [s for s in parts.path.split("/") if s]
        # /us/en/search-results → country "us", language "en"; a site's own prefix may come first.
        pair = next(((c, l) for c, l in zip(segments, segments[1:]) if re.fullmatch(r"[a-z]{2}|global", c) and re.fullmatch(r"[a-z]{2}", l)), None)
        country, lang = pair or ("us", "en")
        self.country = self.options.get("country", country)
        self.lang = self.options.get("lang", f"{lang}_{self.country}")
        self.prefix = "/" + "/".join(segments[: segments.index(pair[0])] + list(pair)) if pair else "/us/en"

    def _body(self, start: int) -> dict[str, Any]:
        return {
            "lang": self.lang, "deviceType": "desktop", "country": self.country, "pageName": "search-results",
            "ddoKey": "refineSearch", "sortBy": "Most recent", "subsearch": "", "from": start, "jobs": True,
            "counts": False, "all_fields": [], "size": PAGE_SIZE, "clearAll": False, "jdsource": "facets",
            "isSliderEnable": False, "pageId": "page1", "siteType": "external", "keywords": self.options.get("keywords", ""),
            "global": True, "selected_fields": self.options.get("selected_fields") or {}, "locationData": {},
        }

    async def fetch_jobs(self) -> ScrapeResult:
        cap = int(self.options.get("max_jobs", 3000))
        jobs: dict[str, JobPosting] = {}
        start = 0
        while start < cap:
            data = await self.http.post_json(f"{self.origin}/widgets", self._body(start))
            search = (data or {}).get("refineSearch") if isinstance(data, dict) else None
            if not isinstance(search, dict) or not isinstance((search.get("data") or {}).get("jobs"), list):
                raise ScraperError("unexpected Phenom response shape")
            total = search.get("totalHits")
            if not isinstance(total, int) or total < 0:
                raise ScraperError("Phenom listing has no valid total")
            before = len(jobs)
            for item in search["data"]["jobs"]:
                job = self.parse_job(item)
                jobs[job.job_id] = job
            if len(jobs) >= total:
                return ScrapeResult(list(jobs.values()))
            if len(jobs) == before:
                return ScrapeResult(list(jobs.values()), complete=False)
            start += PAGE_SIZE
        return ScrapeResult(list(jobs.values()), complete=False)

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id = str(item.get("jobSeqNo") or item.get("jobId") or "")
        title = (item.get("title") or "").strip()
        if not job_id or not title:
            raise ScraperError("Phenom job missing id or title")
        places = [p for p in item.get("multi_location") or [] if p]
        if len(places) < 2:
            places = [item.get("cityStateCountry") or join_nonempty(item.get("city"), item.get("state"), item.get("country"))
                      or item.get("location") or ""]
        return self.make_job(
            job_id=job_id, title=title,
            url=f"{self.origin}{self.prefix}/job/{item.get('jobId') or job_id}",
            location="; ".join(p for p in places if p) or None,
            department=item.get("category") or None,
            employment_type=item.get("type") or None,
            date_posted=parse_date(item.get("postedDate") or item.get("dateCreated")),
        )

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        found = _DDO.search(await self.http.get_text(job.url))
        if not found:
            return None
        try:
            ddo = json.loads(found[1])
        except ValueError:
            return None
        return (((ddo.get("jobDetail") or {}).get("data") or {}).get("job") or {}).get("description")
