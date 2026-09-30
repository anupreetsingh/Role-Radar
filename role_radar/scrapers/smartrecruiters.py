"""SmartRecruiters public career pages (careers.smartrecruiters.com/{company}).

The posting API (api.smartrecruiters.com) is off limits: its robots.txt allows only
LinkedIn's crawler. The public career page lists the same jobs, grouped by location,
loading a few groups at a time as it's scrolled:

  GET https://careers.smartrecruiters.com/{company}/api/groups?page=0..
      a page of location groups (HTML); empty after the last. These answer even for a
      company whose career page redirects to its own site (SEEK, Nearmap), and 404
      for an unknown company.
  GET https://careers.smartrecruiters.com/{company}/api/more?type=location&value={v}&page=1..
      a group's jobs past its first 9, from its "Show more jobs" link (empty when done)
  GET https://jobs.smartrecruiters.com/{company}/{id}-{slug}   (Accept: application/json)
      one job: its description sections and posting date

A group's heading is its jobs' location (a job is listed under its main location; one
found in two groups would be merged by ID, keeping both). The listing has no posting
date or description; the job's own page is read only for the experience check.
"""

from __future__ import annotations

import re
from html import unescape

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date

SITE = "https://careers.smartrecruiters.com"
_GROUP = re.compile(r'<section\b[^>]*\bdata-qty="(\d+)"[^>]*>(.*?)</section>', re.S)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
# The job's sections on what the role needs; not the company blurb or the benefits.
_ROLE_SECTIONS = ("jobDescription", "qualifications")


def _attr(attrs: str | None, name: str) -> str | None:
    match = re.search(rf'(?<![\w-]){name}="([^"]*)"', attrs or "")
    return unescape(match[1]) if match else None


def _has_class(attrs: str, class_name: str) -> bool:
    return class_name in (_attr(attrs, "class") or "").split()


def _tag_attrs(html: str, class_name: str) -> str | None:
    """The attributes of the first tag with `class_name` among its classes."""
    return next((m[1] for m in re.finditer(r"<\w+\b([^>]*)>", html) if _has_class(m[1], class_name)), None)


class SmartRecruitersScraper(BaseScraper):
    name = "smartrecruiters"
    domains = ("smartrecruiters.com",)

    @property
    def identifier(self) -> str:
        """The company's name in its URLs ("Canva", "Nagarro1")."""
        identifier = self.options.get("company_identifier") or first_path_segment(self.company.url)
        if not identifier:
            raise ScraperError("could not determine SmartRecruiters company; set options.company_identifier")
        return identifier

    async def fetch_jobs(self) -> ScrapeResult:
        jobs: dict[str, JobPosting] = {}
        ended, whole_groups = False, True
        for number in range(int(self.options.get("max_pages", 100))):
            page = await self.http.get_text(f"{SITE}/{self.identifier}/api/groups?page={number}")
            if not page.strip():
                ended = True
                break
            groups = _GROUP.findall(page)
            if not groups:
                raise ScraperError("SmartRecruiters job groups missing; refusing to treat the page as the end")
            for qty, body in groups:
                whole_groups &= await self._read_group(jobs, int(qty), body)
        for job in jobs.values():
            job.location = "; ".join(job.extra.pop("places")) or None
            if job.extra.pop("remote") and job.location and "remote" not in job.location.lower():
                job.location += " (Remote)"
        return ScrapeResult(list(jobs.values()), complete=ended and whole_groups)

    async def _read_group(self, jobs: dict[str, JobPosting], qty: int, body: str) -> bool:
        """Add a location group's jobs, following its "Show more jobs" pages. False if some are missing."""
        heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", body, re.S)
        location = html_to_text(heading[1]) if heading else None
        ids = set(self._add_cards(jobs, body, location))
        link = _tag_attrs(body, "js-more")
        if link:
            path, kind, value = (_attr(link, n) for n in ("data-more", "data-type", "data-value"))
            if kind != "location":
                raise ScraperError(f"jobs are grouped by {kind!r}, not location")
            for number in range(1, int(self.options.get("max_more_pages", 100)) + 1):
                # data-value comes URL-encoded ("Bengaluru%2C%20IN"), as the page's own script sends it.
                page = await self.http.get_text(f"{SITE}{path}?type={kind}&value={value}&page={number}")
                listed = self._add_cards(jobs, page, location)
                if not set(listed) - ids:  # the end (an empty page), or a page repeating the last
                    break
                ids.update(listed)
        return len(ids) >= qty

    def _add_cards(self, jobs: dict[str, JobPosting], html: str, location: str | None) -> list[str]:
        """Add the job cards in `html` under `location`; returns every listed job's ID."""
        listed = []
        for attrs, body in _ANCHOR.findall(html):
            if not _has_class(attrs, "js-job-ad-link"):
                continue
            url = _attr(attrs, "href") or ""
            match = re.search(r"smartrecruiters\.com/[^/]+/(\d+)", url)
            if not match:
                raise ScraperError(f"SmartRecruiters job link without an ID: {url!r}")
            job_id = match[1]
            listed.append(job_id)
            job = jobs.get(job_id)
            if job is None:
                title = re.search(r"<h4\b[^>]*>(.*?)</h4>", body, re.S)
                kind = re.search(r'<span class="margin--right--s">(.*?)</span>', body, re.S)
                job = jobs[job_id] = self.make_job(
                    job_id=job_id,
                    title=html_to_text(title[1] if title else body),
                    url=url,
                    employment_type=html_to_text(kind[1]) if kind else None,
                    extra={"places": [], "remote": "work remotely" in body},
                )
            if location and location not in job.extra["places"]:
                job.extra["places"].append(location)
        return listed

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        data = await self.http.get_json(job.url)  # the job page answers JSON to a JSON Accept header
        data = data if isinstance(data, dict) else {}
        job.date_posted = job.date_posted or parse_date(data.get("postedDate"))
        sections = (data.get("content") or {}).get("sections") or {}
        texts = [(sections.get(name) or {}).get("text") or "" for name in _ROLE_SECTIONS]
        return "".join(texts) or None
