"""Fallback for custom careers pages, tried in this order:

  1. Embedded ATS: if the page embeds a Greenhouse/Lever/Ashby/BambooHR/Workday
     board, hand off to that ATS's JSON API scraper.
  2. schema.org JobPosting JSON-LD blocks (common on SEO-friendly sites).
  3. Link heuristic: anchors whose href matches options.link_pattern.

Only the server-rendered HTML is read. Pages that need JavaScript to render
their jobs usually load them from an API; point the config at that API's ATS
instead.

Every listed job already has a title and URL, so a job's own page is skipped
unless the company's filter needs a location or employment type the listing
didn't give; then its JSON-LD is read for those (never the description).
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

from config import CompanyConfig
from models import JobPosting, html_to_text, normalize_url
from scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

log = logging.getLogger(__name__)

DEFAULT_LINK_PATTERN = r"/(jobs?|careers?|positions?|openings?|vacanc(y|ies)|requisitions?|roles?)/[^/?#]+"
_GENERIC_LINK_TEXT = re.compile(r"^(apply( now)?|learn more|view( all)?( jobs| openings)?|see (all|more)|details|read more|careers?|jobs?)$", re.I)

# Patterns that reveal an embedded ATS board → the canonical careers URL for it.
EMBED_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)"), "https://boards.greenhouse.io/{0}"),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?!embed)([\w-]+)"), "https://boards.greenhouse.io/{0}"),
    ("lever", re.compile(r"jobs\.(eu\.)?lever\.co/([\w.-]+)"), "https://jobs.{0}lever.co/{1}"),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"), "https://jobs.ashbyhq.com/{0}"),
    ("bamboohr", re.compile(r"//(?!www\.)([\w-]+)\.bamboohr\.com"), "https://{0}.bamboohr.com/careers"),
    (
        "workday",
        re.compile(r"//([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
        "https://{0}/{1}",
    ),
]


class _PageParser(HTMLParser):
    """Collects JSON-LD script bodies and (href, text) anchor pairs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_ld = False
        self._ld_buf: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._ld_buf = True, []
        elif tag == "a" and a.get("href"):
            self._href, self._text = a["href"], []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self.json_ld.append("".join(self._ld_buf))
            self._in_ld = False
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._ld_buf.append(data)
        elif self._href is not None:
            self._text.append(data)


def iter_job_postings(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every JSON-LD object with @type JobPosting, however deeply nested."""
    if isinstance(node, list):
        for item in node:
            yield from iter_job_postings(item)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from iter_job_postings(node[key])


def _ld_location(posting: dict[str, Any]) -> str | None:
    locs = posting.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    names = []
    for loc in locs:
        addr = (loc or {}).get("address") or {} if isinstance(loc, dict) else {}
        if isinstance(addr, str):
            names.append(addr)
        elif isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            names.append(join_nonempty(addr.get("addressLocality"), addr.get("addressRegion"), country))
    location = "; ".join(n for n in names if n) or None
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        location = f"{location} (Remote)" if location else "Remote"
    return location


class GenericScraper(BaseScraper):
    name = "generic"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._delegate: BaseScraper | None = None

    async def fetch_jobs(self) -> ScrapeResult:
        page = await self.http.get_text(self.company.url)

        if self.options.get("detect_embedded_ats", True):
            self._delegate = self._embedded_scraper(page)
            if self._delegate:
                log.info("[%s] found embedded %s board; using its API", self.company.name, self._delegate.name)
                return await self._delegate.fetch_jobs()

        parser = _PageParser()
        try:
            parser.feed(page)
        except Exception as exc:  # html.parser is lenient, but never let a bad page crash the run
            raise ScraperError(f"could not parse HTML: {exc}") from exc

        jobs = self._from_json_ld(parser.json_ld, self.company.url)
        if jobs:
            return ScrapeResult(jobs)
        jobs = self._from_links(parser.links)
        if not jobs:
            raise ScraperError("no jobs found in page (JS-rendered? set options.link_pattern or use an ATS scraper)")
        # Heuristic results may be partial, so don't infer removals from them
        # unless the config explicitly says the pattern is reliable.
        return ScrapeResult(jobs, complete=bool(self.options.get("link_pattern")))

    def missing_fields(self, job: JobPosting) -> set[str]:
        if self._delegate:
            return self._delegate.missing_fields(job)
        return {name for name, value in (("location", job.location), ("employment_type", job.employment_type)) if not value}

    async def fetch_details(self, job: JobPosting) -> JobPosting:
        if self._delegate:
            return await self._delegate.fetch_details(job)
        page = await self.http.get_text(job.url)
        parser = _PageParser()
        parser.feed(page)
        for posting in self._postings(parser.json_ld):
            job.date_posted = parse_date(posting.get("datePosted")) or job.date_posted
            job.employment_type = job.employment_type or _employment_type(posting)
            job.location = job.location or _ld_location(posting)
            break
        return job

    # -- strategies --------------------------------------------------------

    def _embedded_scraper(self, page: str) -> BaseScraper | None:
        from scrapers import SCRAPERS  # local import: registry imports this module

        for ats, pattern, template in EMBED_PATTERNS:
            m = pattern.search(page)
            if m:
                url = template.format(*[g or "" for g in m.groups()])
                cfg = CompanyConfig(name=self.company.name, url=url, filter=self.company.filter, ats=ats)
                return SCRAPERS[ats](cfg, self.http)
        return None

    @staticmethod
    def _postings(blocks: list[str]) -> Iterator[dict[str, Any]]:
        for raw in blocks:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            yield from iter_job_postings(data)

    def _from_json_ld(self, blocks: list[str], base_url: str) -> list[JobPosting]:
        jobs = []
        for p in self._postings(blocks):
            title = html_to_text(p.get("title") or p.get("name"))
            if not title:
                continue
            ident = p.get("identifier")
            job_id = ident.get("value") if isinstance(ident, dict) else ident
            jobs.append(
                self.make_job(
                    job_id=str(job_id) if job_id else None,
                    title=title,
                    url=urljoin(base_url, p.get("url") or base_url),
                    location=_ld_location(p),
                    employment_type=_employment_type(p),
                    date_posted=parse_date(p.get("datePosted")),
                )
            )
        return jobs

    def _from_links(self, links: list[tuple[str, str]]) -> list[JobPosting]:
        pattern = re.compile(self.options.get("link_pattern") or DEFAULT_LINK_PATTERN, re.I)
        exclude = self.options.get("exclude_link_pattern")
        exclude_re = re.compile(exclude, re.I) if exclude else None
        base_path = normalize_url(self.company.url)
        seen: dict[str, JobPosting] = {}
        for href, text in links:
            url = urljoin(self.company.url, href)
            if urlsplit(url).scheme not in ("http", "https") or normalize_url(url) == base_path:
                continue
            if not pattern.search(url) or (exclude_re and exclude_re.search(url)):
                continue
            if len(text) < 4 or len(text) > 200 or _GENERIC_LINK_TEXT.match(text):
                continue
            seen.setdefault(normalize_url(url), self.make_job(title=text, url=url))
        return list(seen.values())


def _employment_type(posting: dict[str, Any]) -> str | None:
    value = posting.get("employmentType")
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return value.replace("_", " ").title() if isinstance(value, str) and value else None
