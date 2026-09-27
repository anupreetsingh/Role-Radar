"""amazon.jobs, via search.json (the JSON the site's own search page loads).

Careers URL: https://www.amazon.jobs/en/search. Results are sorted newest first
and Amazon lists 10,000+ jobs, so a check reads the newest options.max_jobs
(default 500, 100 a request) and isn't a removal snapshot unless it read them all.
Options: country (default "USA"), categories (amazon.jobs category slugs, e.g.
software-development), query (keywords), max_jobs.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, parse_date

PAGE_SIZE = 100
HITS_CAP = 10_000  # amazon.jobs reports at most this many hits


class AmazonScraper(BaseScraper):
    name = "amazon"
    domains = ("amazon.jobs",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        params: list[tuple[str, str | int]] = [("result_limit", PAGE_SIZE), ("sort", "recent")]
        if self.options.get("country", "USA"):
            params.append(("country", self.options.get("country", "USA")))
        if self.options.get("query"):
            params.append(("base_query", self.options["query"]))
        params += [("category[]", c) for c in self.options.get("categories") or []]
        max_jobs = int(self.options.get("max_jobs", 500))

        jobs = {}
        offset = 0
        while offset < max_jobs:
            data = await self.http.get_json(f"{origin}/en/search.json", params=[*params, ("offset", offset)])
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ScraperError("unexpected amazon.jobs response shape")
            hits = data.get("hits")
            for item in data["jobs"]:
                job_id, title = item.get("id_icims") or item.get("id"), (item.get("title") or "").strip()
                if not job_id or not title:
                    continue
                path = item.get("job_path") or f"/en/jobs/{job_id}"
                jobs[str(job_id)] = self.make_job(
                    job_id=str(job_id), title=title, url=origin + path,
                    location=item.get("normalized_location") or item.get("location"),
                    employment_type=item.get("job_schedule_type"),
                    department=item.get("job_category"),
                    date_posted=parse_date(item.get("posted_date")),
                    extra={"description": item.get("basic_qualifications")},  # the preferred ones don't count
                )  # fmt: skip
            offset += PAGE_SIZE
            if not data["jobs"] or (isinstance(hits, int) and offset >= hits):
                return ScrapeResult(list(jobs.values()), complete=isinstance(hits, int) and hits < HITS_CAP)
        return ScrapeResult(list(jobs.values()), complete=False)
