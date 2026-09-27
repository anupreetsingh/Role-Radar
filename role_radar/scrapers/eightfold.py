"""Eightfold AI career sites (Microsoft, Netflix, Qualcomm, PayPal...), via the JSON
search API the site's own pages call.

Careers URL: the site's /careers page, e.g. https://explore.jobs.netflix.net/careers
or https://qualcomm.eightfold.ai/careers. Options:
  domain      the employer domain the API expects (default: the URL's ?domain=, else
              the registrable part of a branded host, else the *.eightfold.ai name + ".com")
  api         "pcsx" (/api/pcsx/search, the default: Microsoft, Qualcomm, PayPal...) or
              "apply" (/api/apply/v2/jobs: Netflix, whose pcsx API is switched off). A
              tenant without the API answers 403 or 404.
  location    default "United States"
  query       keywords
  max_jobs    default 500
Eightfold APIs rate-limit quickly, so give the host a generous delay in
settings.http.host_delays.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date

PAGE_SIZE = 10  # the largest page every Eightfold tenant serves


class EightfoldScraper(BaseScraper):
    name = "eightfold"
    domains = ("eightfold.ai",)

    def _domain(self, host: str) -> str:
        if self.options.get("domain"):
            return self.options["domain"]
        given = parse_qs(urlsplit(self.company.url).query).get("domain")
        if given:
            return given[0]
        if host.endswith(".eightfold.ai"):
            return host.split(".")[0] + ".com"
        return ".".join(host.split(".")[-2:])  # apply.careers.microsoft.com → microsoft.com

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin, host = f"{parts.scheme}://{parts.netloc}", parts.netloc.lower()
        pcsx = self.options.get("api", "pcsx") == "pcsx"
        endpoint = origin + ("/api/pcsx/search" if pcsx else "/api/apply/v2/jobs")
        params: dict[str, Any] = {"domain": self._domain(host), "location": self.options.get("location", "United States"),
                                  "sort_by": "timestamp", "num": PAGE_SIZE}
        if self.options.get("query"):
            params["query"] = self.options["query"]
        max_jobs = int(self.options.get("max_jobs", 500))

        jobs = {}
        start = 0
        while start < max_jobs:
            data = await self.http.get_json(endpoint, params={**params, "start": start})
            body = (data or {}).get("data") if pcsx else data
            positions, count = (body or {}).get("positions"), (body or {}).get("count")
            if not isinstance(positions, list) or not isinstance(count, int):
                raise ScraperError("unexpected Eightfold response shape")
            for item in positions:
                job = self._job(item, origin, pcsx)
                if job:
                    jobs[job.job_id] = job
            start += len(positions)
            if not positions or start >= count:
                return ScrapeResult(list(jobs.values()), complete=start >= count)
        return ScrapeResult(list(jobs.values()), complete=False)

    def _job(self, item: dict[str, Any], origin: str, pcsx: bool):
        title = (item.get("name") or "").strip()
        if item.get("id") is None or not title:
            return None
        if pcsx:
            places, posted, url = item.get("locations") or [], item.get("postedTs"), origin + (item.get("positionUrl") or "")
            job_id = item.get("displayJobId") or item.get("id")
        else:
            places = item.get("locations") or [item.get("location")]
            posted, url = item.get("t_create"), item.get("canonicalPositionUrl") or f"{origin}/careers/job/{item['id']}"
            job_id = item.get("display_job_id") or item.get("ats_job_id") or item.get("id")
        return self.make_job(
            job_id=str(job_id), title=title, url=url if url != origin else f"{origin}/careers/job/{item['id']}",
            location="; ".join(p for p in places if p) or None,
            department=item.get("department"),
            date_posted=parse_date(posted),
        )  # fmt: skip
