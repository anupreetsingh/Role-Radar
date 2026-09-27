"""Public iCIMS job cards, including global boards spanning several subportals.

Request the same compact in_iframe view used by the career site. Follow each
visible Next link, rather than assuming the first page contains every job.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from role_radar.models import html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty
from role_radar.scrapers.generic import _PageParser
from role_radar.scrapers.html_pages import cards, next_link


class ICIMSScraper(BaseScraper):
    name = "icims"
    domains = ("icims.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        query = parse_qs(parts.query)
        query.update({"in_iframe": ["1"], "ss": ["1"]})
        url = urlunsplit((parts.scheme, parts.netloc, "/jobs/search", urlencode(query, doseq=True), ""))
        jobs, visited = {}, set()
        for _ in range(int(self.options.get("max_pages", 100))):
            if url in visited:
                return ScrapeResult(list(jobs.values()), complete=False)
            visited.add(url)
            page = await self.http.get_text(url)
            rows = cards(page, "li", "iCIMS_JobCardItem")
            if not rows:
                if not jobs and re.search(r"(?:no (?:current )?(?:jobs|positions)|0 (?:jobs|results))", html_to_text(page), re.I):
                    return ScrapeResult([])
                raise ScraperError("iCIMS job cards missing; refusing to treat the response as an empty board")
            before = len(jobs)
            for row in rows:
                parser = _PageParser()
                parser.feed(row)
                link = next(((u, t) for u, t in parser.links if re.search(r"/jobs/\d+/", u)), None)
                if not link:
                    raise ScraperError("iCIMS card missing job link")
                link_url, title = link
                heading = re.search(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", row, re.I | re.S)
                title = html_to_text(heading[1]) if heading else re.sub(r"^Requisition Title\s*", "", title)
                fields = {html_to_text(k).lower(): html_to_text(v) for k, v in re.findall(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", row, re.I | re.S)}
                location = join_nonempty(*(v for k, v in fields.items() if any(w in k for w in ("location", "country", "state", "city"))))
                job_id = re.search(r"/jobs/(\d+)/", link_url)[1]
                # An iCIMS hub can contain ids from distinct customer subportals.
                job_key = f"{urlsplit(link_url).hostname}:{job_id}"
                jobs[job_key] = self.make_job(
                    job_id=job_key, title=title, url=link_url.split("?")[0],
                    location=location, department=fields.get("job category") or fields.get("category"),
                    employment_type=fields.get("position type") or fields.get("employment type"),
                )
            following = next_link(page, url)
            if not following:
                return ScrapeResult(list(jobs.values()))
            if len(jobs) == before:
                return ScrapeResult(list(jobs.values()), complete=False)
            url = following
        return ScrapeResult(list(jobs.values()), complete=False)
