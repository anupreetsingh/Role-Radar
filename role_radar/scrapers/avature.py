"""Avature's public SearchJobs result cards (e.g. Bloomberg, Siemens, Two Sigma).

Career sites use different schemas; this adapter validates the supported
article--result layout and follows the site's actual Next links.

A search URL sorted newest first (Siemens: SearchJobs/?folderSort=postedDate&folderSortDirection=DESC,
6 jobs a page, the sort kept in its Next links) is read from the top only until a page holds
no job seen before, up to options.max_pages; a company's first check reads all of them. A job
the site reposts moves back to the top. Such a read is never a removal snapshot. A card that
says only "Multiple Locations" has its job page read for the places when the job could match,
and the experience filter reads a new match's description from that same page.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError
from role_radar.scrapers.generic import _PageParser
from role_radar.scrapers.html_pages import cards, next_link

_MULTI_LOCATION = re.compile(r"multiple locations$", re.I)


class AvatureScraper(BaseScraper):
    name = "avature"
    domains = ("avature.net",)

    async def fetch_jobs(self) -> ScrapeResult:
        url = self.company.url
        newest_first = _newest_first(url)
        jobs, visited = {}, set()
        for _ in range(int(self.options.get("max_pages", 150))):
            if url in visited:
                return ScrapeResult(list(jobs.values()), complete=False)
            visited.add(url)
            page = await self.http.get_text(url)
            rows = cards(page, "article", "article--result")
            if not rows:
                if not jobs and re.search(r"no (?:jobs|results|positions) (?:found|match|available)", html_to_text(page), re.I):
                    return ScrapeResult([])
                raise ScraperError("unsupported or missing Avature result cards")
            before, listed = len(jobs), []
            for row in rows:
                parser = _PageParser()
                parser.feed(row)
                link = next(((u, t) for u, t in parser.links if "/JobDetail/" in u), None)
                if not link:
                    raise ScraperError("Avature card missing job link")
                link_url, title = link
                job_id = link_url.rstrip("/").rsplit("/", 1)[-1]
                # Siemens nests the city, state and country in spans of their own;
                # Two Sigma's card gives the place first, then the team and level.
                location = _inner_html(row, "span", "list-item-location") or _inner_html(row, "span", "paragraph_inner-span")[:1]
                jobs[job_id] = self.make_job(job_id=job_id, title=title, url=link_url, location=html_to_text(location[0]) if location else None)
                listed.append(jobs[job_id])
            following = next_link(page, url)
            if not following:
                return ScrapeResult(list(jobs.values()))
            if len(jobs) == before:
                return ScrapeResult(list(jobs.values()), complete=False)
            if newest_first and self.known and all(job.uid in self.known for job in listed):
                return ScrapeResult(list(jobs.values()), complete=False)
            url = following
        return ScrapeResult(list(jobs.values()), complete=False)

    def missing_fields(self, job: JobPosting) -> set[str]:
        return {"location"} if job.location and _MULTI_LOCATION.match(job.location.strip()) else set()

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        page = await self.http.get_text(job.url)
        job.extra["description"] = _description(page)
        # "Beijing - Beijing Shi - China", one list item per place
        places = [html_to_text(li).replace(" - ", ", ") for ul in _inner_html(page, "ul", "list--locations") for li in _inner_html(ul, "li", "list__item")]
        if any(places):
            job.location = "; ".join(p for p in places if p)
        return job

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        if "description" not in job.extra:  # its page wasn't read for the location
            job.extra["description"] = _description(await self.http.get_text(job.url))
        return job.extra["description"]


def _newest_first(url: str) -> bool:
    """Whether a search URL sorts by posting date, newest first (folderSort=postedDate&folderSortDirection=DESC)."""
    query = {k.lower(): v.lower() for k, v in parse_qsl(urlsplit(url).query)}
    return any(k.endswith("sort") and v == "posteddate" for k, v in query.items()) and any(
        k.endswith("sortdirection") and v == "desc" for k, v in query.items()
    )


def _inner_html(page: str, tag: str, class_name: str) -> list[str]:
    """The contents of each <tag> with `class_name` among its classes, nested <tag>s included."""
    parts = []
    for opening in re.finditer(rf'''<{tag}\b[^>]*\bclass=["'](?:[^"']*\s)?{re.escape(class_name)}(?:\s[^"']*)?["'][^>]*>''', page, re.I):
        depth, at = 1, opening.end()
        for found in re.finditer(rf"<(/?){tag}\b[^>]*>", page[at:], re.I):
            depth += -1 if found[1] else 1
            if not depth:
                parts.append(page[at : at + found.start()])
                break
    return parts


def _description(page: str) -> str | None:
    """The text of a job page's fields (Siemens: job ID, experience level, locations, then the description)."""
    return "\n".join(t for t in map(html_to_text, _inner_html(page, "div", "article__content__view__field__value")) if t) or None
