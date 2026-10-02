"""SAP SuccessFactors career sites (Career Site Builder), e.g. jobs.aa.com or careers.ey.com.

These sites live on the company's own domain, so a company names this reader with
`ats: successfactors` (only *.jobs2web.com hosts are recognised from the URL). The sites'
job search API (/services/) is closed to crawlers by their robots.txt; what's open:

  GET {site}/sitemap.xml
      every open job, as either a job feed (RSS items: title, location, category and
      description) or a plain list of job links, /job/{city-title-state-zip}/{id}/
  GET {site}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow={n}
      the classic search page: rows of title, location and (on some sites) date, 10 to
      100 a page. Newer site designs fill it in the browser and leave no rows.
  GET {site}/job/{slug}/{id}/
      one job: its title, location, posting date and description

A check reads the sitemap, which is the whole listing, and then the search pages from the
top for the titles of jobs not seen before, up to options.max_pages pages (default 5); the
first page is always read, so a job posted since the sitemap was built is caught too. A job
already on record keeps its stored title and location. One still without a title is
listed under its link's words ("Fort Worth Engineer IT AI TX 76101"), which contain the
whole title, so the title keywords can still screen it: one that could match has its own
page read for the real title and location before it's matched (fetch_details). Jobs listed
at a company's first check are a baseline that isn't alerted, so their pages aren't read.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape
from urllib.parse import quote, unquote, urljoin, urlsplit

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

SEARCH = "/search/?q={q}&sortColumn=referencedate&sortDirection=desc&startrow={start}"
MAX_SITEMAPS = 10  # child sitemaps followed from a sitemap index
_JOB_LINK = re.compile(r"/job/([^/?#]+)/(\d+)(?:-[a-z]{2}_[A-Z]{2})?/?(?:[?#]|$)")
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_ROW = re.compile(r'<tr\b[^>]*\bclass="data-row\b[^"]*"[^>]*>(.*?)</tr>', re.S)
_TITLE_LINK = re.compile(r'<a\b(?=[^>]*\bclass="jobTitle-link\b)[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', re.S)
_TOTAL = re.compile(r'class="paginationLabel"[^>]*>.*?\bof\s*<b>\s*([\d,.]+)\s*</b>', re.S)


def _tag(xml: str, name: str) -> str | None:
    """The text of the first <name> element (CDATA unwrapped, entities decoded), or None."""
    match = re.search(rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>", xml, re.S)
    if not match:
        return None
    text = match[1].strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", text, re.S)
    return cdata[1] if cdata else unescape(text)


def _span(html: str, class_name: str) -> str | None:
    """The text of the first <span> with `class_name` among its classes."""
    match = re.search(rf'<span\b[^>]*\bclass="(?:[^"]*\s)?{class_name}(?:\s[^"]*)?"[^>]*>(.*?)</span>', html, re.S)
    if not match:
        return None
    return html_to_text(re.sub(r"<small\b.*?</small>", "", match[1], flags=re.S)) or None


def _meta(html: str, prop: str) -> str | None:
    match = re.search(rf'<meta\b(?=[^>]*\b(?:itemprop|property)="{re.escape(prop)}")[^>]*\bcontent="([^"]*)"', html)
    if not match:
        return None
    return unescape(match[1]).strip() or None


def _posted(value: str | None) -> date | None:
    """A job page's datePosted ("Fri Oct 02 07:00:00 UTC 2026"), or another format parse_date knows."""
    if value:
        try:
            return datetime.strptime(re.sub(r" [A-Z]{2,5} (?=\d{4}$)", " ", value), "%a %b %d %H:%M:%S %Y").date()
        except ValueError:
            pass
    return parse_date(value)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text)


def link_words(slug: str) -> str:
    """A job link's words: "Fort-Worth-Engineer%2C-IT-AI-TX-76101" → "Fort Worth Engineer IT AI TX 76101"."""
    return " ".join(_words(unquote(slug)))


def link_location(slug: str, title: str) -> str | None:
    """The place around the title in a job link: ("Fort-Worth-Engineer%2C-IT-AI-TX-76101",
    "Engineer, IT AI") → "Fort Worth, TX, 76101". None if the title isn't in the link."""
    words, wanted = _words(unquote(slug)), [w.lower() for w in _words(title)]
    lowered = [w.lower() for w in words]
    for start in range(len(words) - len(wanted) + 1):
        if wanted and lowered[start : start + len(wanted)] == wanted:
            city = " ".join(words[:start])
            return join_nonempty(city, *words[start + len(wanted) :]) or None
    return None


class SuccessFactorsScraper(BaseScraper):
    name = "successfactors"
    domains = ("jobs2web.com",)

    @property
    def origin(self) -> str:
        parts = urlsplit(self.company.url)
        if not parts.netloc:
            raise ScraperError(f"not a career site URL: {self.company.url!r}")
        return f"{parts.scheme or 'https'}://{parts.netloc}"

    async def fetch_jobs(self) -> ScrapeResult:
        jobs = await self._read_sitemap(self.options.get("sitemap") or f"{self.origin}/sitemap.xml")
        listed = set(jobs)
        for job_id, job in list(jobs.items()):
            known = self.known.get(job.uid)
            if job.extra.get("from_link") and known and known.title:
                jobs[job_id] = self.make_job(job_id=job_id, title=known.title, url=job.url, location=known.location)
        behind = await self._read_search(jobs)
        # A job the search shows but the sitemap doesn't means the sitemap is behind:
        # then jobs missing from it may still be open, so the listing isn't a snapshot.
        return ScrapeResult(list(jobs.values()), complete=not behind - listed)

    async def _read_sitemap(self, url: str, depth: int = 0) -> dict[str, JobPosting]:
        xml = await self.http.get_text(url)
        jobs: dict[str, JobPosting] = {}
        if "<sitemapindex" in xml and depth == 0:
            children = [u for u in _LOC.findall(xml) if urlsplit(u).netloc == urlsplit(url).netloc]
            for child in children[:MAX_SITEMAPS]:
                jobs.update(await self._read_sitemap(unescape(child), depth + 1))
            return jobs
        if "<rss" in xml and "<channel" in xml:
            for item in _ITEM.findall(xml):
                job = self._from_item(item)
                jobs[job.job_id] = job
            return jobs
        if "<urlset" not in xml:
            raise ScraperError("the career site's sitemap.xml is neither a job feed nor a list of links")
        for loc in _LOC.findall(xml):
            loc = unescape(loc)
            match = _JOB_LINK.search(urlsplit(loc).path)
            if match and match[2] not in jobs:
                jobs[match[2]] = self.make_job(job_id=match[2], title=link_words(match[1]), url=loc, extra={"from_link": True})
        return jobs

    def _from_item(self, item: str) -> JobPosting:
        url = _tag(item, "link") or ""
        match = _JOB_LINK.search(urlsplit(url).path)
        job_id = _tag(item, "g:id") or _tag(item, "guid") or (match[2] if match else None)
        title, location = _tag(item, "title") or "", _tag(item, "g:location")
        if not job_id or not title:
            raise ScraperError(f"job feed item without an ID or title: {url!r}")
        if location and title.endswith(f" ({location})"):  # "Engineer, IT AI (Fort Worth, TX, US)"
            title = title[: -len(location) - 3]
        return self.make_job(
            job_id=job_id, title=html_to_text(title), url=url, location=location,
            department=_tag(item, "g:job_function"), extra={"description": _tag(item, "description")},
        )  # fmt: skip

    async def _read_search(self, jobs: dict[str, JobPosting]) -> set[str]:
        """Give jobs listed by their links the titles the search pages show; add jobs only the
        search shows. Returns the IDs on the pages read."""
        seen: set[str] = set()
        start = 0
        for _ in range(max(int(self.options.get("max_pages", 5)), 1)):
            found, total = await self.search(start=start)
            for job in found:
                seen.add(job.job_id)
                if job.job_id not in jobs or jobs[job.job_id].extra.get("from_link"):
                    jobs[job.job_id] = job
            start += len(found)
            untitled = any(job.extra.get("from_link") for job in jobs.values())
            if not found or not untitled or (total is not None and start >= total):
                break
        return seen

    async def search(self, keywords: str = "", start: int = 0) -> tuple[list[JobPosting], int | None]:
        """A page of the classic search, newest first, and how many jobs the search found (None if
        the page doesn't say). No jobs where the site fills its search pages in the browser."""
        page = await self.http.get_text(self.origin + SEARCH.format(q=quote(keywords), start=start))
        total = _TOTAL.search(page)
        found = [job for row in _ROW.findall(page) if (job := self._from_row(row))]
        return found, int(re.sub(r"\D", "", total[1])) if total else None

    def _from_row(self, row: str) -> JobPosting | None:
        link = _TITLE_LINK.search(row)
        match = link and _JOB_LINK.search(urlsplit(unescape(link[1])).path)
        if not match:
            return None
        return self.make_job(
            job_id=match[2], title=html_to_text(link[2]), url=urljoin(self.origin, unescape(link[1])),
            location=_span(row, "jobLocation"), department=_span(row, "jobDepartment"),
            date_posted=parse_date(_span(row, "jobDate")),
        )  # fmt: skip

    # -- a job's own page -----------------------------------------------------------------

    def missing_fields(self, job: JobPosting) -> set[str]:
        return {"location"} if job.extra.get("from_link") else set()

    def wants_details(self, job: JobPosting) -> bool:
        # A job known only by its link: its page has the real title. Not at the first check,
        # whose jobs are a baseline (self.known is empty then).
        return bool(job.extra.get("from_link") and self.known)

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        page = await self.http.get_text(job.url)
        title = re.search(r'<(?:h1|span)\b[^>]*\bitemprop="title"[^>]*>(.*?)</(?:h1|span)>', page, re.S)
        title = html_to_text(title[1]) if title else _meta(page, "og:title")
        if not title:
            raise ScraperError(f"no title on the job page {job.url}")
        slug = _JOB_LINK.search(urlsplit(job.url).path)
        job.location = (
            join_nonempty(_meta(page, "addressLocality"), _meta(page, "addressRegion"), _meta(page, "addressCountry"))
            or _span(page, "jobGeoLocation") or (link_location(slug[1], title) if slug else None)
        )  # fmt: skip
        job.title = title
        job.date_posted = job.date_posted or _posted(_meta(page, "datePosted"))
        job.extra.update(from_link=False, description=_description(page))
        return job

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        if job.extra.get("description"):
            return job.extra["description"]
        return _description(await self.http.get_text(job.url))


def _description(page: str) -> str | None:
    """The text of a job page's description parts (itemprop="description" spans, which nest)."""
    parts = []
    for opening in re.finditer(r'<span\b[^>]*\bitemprop="description"[^>]*>', page):
        depth, at = 1, opening.end()
        for tag in re.finditer(r"<(/?)span\b[^>]*>", page[at:]):
            depth += -1 if tag[1] else 1
            if not depth:
                parts.append(html_to_text(page[at : at + tag.start()]))
                break
    return "\n".join(p for p in parts if p) or None
