"""ADP Workforce Now career centers (Lake Louise Ski Resort, the Calgary Zoo, Westmont Hospitality...),
via the public job list their own pages call.

Careers URL: https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}[&ccId=..][&lang=en_CA]
  GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
      ?cid={cid}&lang=..&locale=..&$top=100&$skip=N      every job, newest first, with its places
  GET .../job-requisitions/{id}?cid={cid}&lang=..       one job's description, read only for the experience check
Options: cid, cc_id, lang (default en_US), max_jobs (default 1000).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import COUNTRY_NAMES, BaseScraper, ScrapeResult, ScraperError, parse_date

HOST = "https://workforcenow.adp.com"
API = f"{HOST}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PAGE_SIZE = 100
_COUNTRY_CODE = re.compile(r",\s*([A-Z]{2})$")


def _place(name: str | None) -> str | None:
    """ "Lake Louise, AB, CA" → "Lake Louise, AB, Canada": the trailing code is the country's."""
    text = " ".join((name or "").split()).strip(" ,")
    if not text:
        return None
    match = _COUNTRY_CODE.search(text)
    if match and match[1] in COUNTRY_NAMES:
        text = text[: match.start()] + ", " + COUNTRY_NAMES[match[1]]
    return text


class ADPScraper(BaseScraper):
    name = "adp"
    domains = ("workforcenow.adp.com",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        query = {k.lower(): v[0] for k, v in parse_qs(urlsplit(self.company.url).query).items() if v}
        self.cid = str(self.options.get("cid") or query.get("cid") or "")
        if not self.cid:
            raise ScraperError("could not determine the ADP career center (cid) from the URL; set options.cid")
        self.cc_id = self.options.get("cc_id") or query.get("ccid")
        self.lang = str(self.options.get("lang") or query.get("lang") or "en_US")

    def _query(self, **extra: Any) -> str:
        return urlencode({"cid": self.cid, "lang": self.lang, "locale": self.lang, **extra})

    async def fetch_jobs(self) -> ScrapeResult:
        max_jobs = int(self.options.get("max_jobs", 1000))
        jobs: dict[str, JobPosting] = {}
        skip = 0
        while skip < max_jobs:
            data = await self.http.get_json(f"{API}?{self._query(**{'$top': PAGE_SIZE, '$skip': skip})}")
            items = (data or {}).get("jobRequisitions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ScraperError("unexpected ADP response shape")
            for item in items:
                if item.get("itemID") and item.get("requisitionTitle"):
                    job = self.parse_job(item)
                    jobs.setdefault(job.uid, job)
            total = int(((data.get("meta") or {}).get("totalNumber")) or 0)
            skip += len(items)
            if not items or skip >= total:
                return ScrapeResult(list(jobs.values()))
        return ScrapeResult(list(jobs.values()), complete=False)

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        job_id = str(item["itemID"])
        places = [_place((loc.get("nameCode") or {}).get("shortName")) for loc in item.get("requisitionLocations") or []]
        link = {"cid": self.cid, **({"ccId": self.cc_id} if self.cc_id else {}), "jobId": job_id, "lang": self.lang}
        return self.make_job(
            job_id=job_id,
            title=" ".join((item.get("requisitionTitle") or "").split()),
            url=f"{HOST}/mascsr/default/mdf/recruitment/recruitment.html?{urlencode(link)}",
            location="; ".join(dict.fromkeys(p for p in places if p)) or None,
            employment_type=(item.get("workLevelCode") or {}).get("shortName"),
            date_posted=parse_date(item.get("postDate")),
        )

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        data = await self.http.get_json(f"{API}/{job.job_id}?{self._query()}")
        return (data or {}).get("requisitionDescription") if isinstance(data, dict) else None
