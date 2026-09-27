"""Oracle Cloud HCM Candidate Experience sites (JPMorgan Chase, Oracle, Texas
Instruments, Honeywell...), via the public REST API the site's own pages call.

Careers URL: https://{host}/hcmUI/CandidateExperience/en/sites/{site}/... (any page of
the site; the site number comes from the path, or options.site).
  GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
      ?onlyData=true&expand=requisitionList.secondaryLocations
      &finder=findReqs;siteNumber={site},limit=N,offset=N,sortBy=POSTING_DATES_DESC
Results are newest first; a check reads options.max_jobs (default 500) and is a
removal snapshot only when that covers them all. Options: site, keyword,
location_id (the site's ID for a country or city), max_jobs, page_size (default 100).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

_SITE = re.compile(r"/sites/([^/?#]+)")


class OracleHcmScraper(BaseScraper):
    name = "oracle_hcm"
    domains = ("oraclecloud.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        m = _SITE.search(parts.path)
        site = self.options.get("site") or (m.group(1) if m else None)
        if not site:
            raise ScraperError("could not determine the Oracle HCM site number; set options.site")
        page_size = int(self.options.get("page_size", 100))
        max_jobs = int(self.options.get("max_jobs", 500))
        extra = ""
        if self.options.get("keyword"):
            extra += f',keyword="{quote(str(self.options["keyword"]))}"'
        if self.options.get("location_id"):
            extra += f",locationId={self.options['location_id']}"

        jobs = {}
        offset = 0
        while offset < max_jobs:
            # The finder's ; , = must reach Oracle unencoded, so the URL is built by hand.
            url = (f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                   f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
                   f"limit={page_size},offset={offset},sortBy=POSTING_DATES_DESC{extra}")
            data = await self.http.get_json(url)
            items = (data or {}).get("items")
            if not isinstance(items, list) or not items or not isinstance(items[0].get("requisitionList"), list):
                raise ScraperError("unexpected Oracle HCM response shape")
            reqs, total = items[0]["requisitionList"], items[0].get("TotalJobsCount")
            for item in reqs:
                job = self._job(item, origin, site)
                if job:
                    jobs[job.job_id] = job
            offset += len(reqs)
            if not reqs or (isinstance(total, int) and offset >= total):
                return ScrapeResult(list(jobs.values()), complete=isinstance(total, int) and offset >= total)
        return ScrapeResult(list(jobs.values()), complete=False)

    def _job(self, item: dict[str, Any], origin: str, site: str):
        job_id, title = item.get("Id"), (item.get("Title") or "").strip()
        if not job_id or not title:
            return None
        others = [loc.get("Name") for loc in item.get("secondaryLocations") or [] if loc.get("Name")]
        return self.make_job(
            job_id=str(job_id), title=title,
            url=f"{origin}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}",
            location=join_nonempty(item.get("PrimaryLocation"), *others, sep="; "),
            employment_type=item.get("JobSchedule") or item.get("JobType"),
            department=item.get("JobFamily") or item.get("Organization"),
            date_posted=parse_date(item.get("PostedDate")),
        )  # fmt: skip
