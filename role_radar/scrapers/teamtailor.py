"""Teamtailor career sites ({company}.teamtailor.com, or the company's own domain, e.g. GitGuardian).

  GET {origin}/jobs.rss

lists every open job: title, link, publication date, remote status, department, places (city
and country, in <tt:locations>) and description (kept in memory, for this check, only for the
experience filter). One request a check.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty

TT = "{https://teamtailor.com/locations}"


class TeamtailorScraper(BaseScraper):
    name = "teamtailor"
    domains = ("teamtailor.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        try:
            channel = ET.fromstring(await self.http.get_text(f"{parts.scheme}://{parts.netloc}/jobs.rss")).find("channel")
        except ET.ParseError as exc:
            raise ScraperError(f"Teamtailor feed isn't XML: {exc}") from exc
        if channel is None:
            raise ScraperError("Teamtailor feed has no channel")
        return ScrapeResult([self.parse_item(item) for item in channel.findall("item")])

    def parse_item(self, item: ET.Element) -> JobPosting:
        link, title = (item.findtext("link") or "").strip(), (item.findtext("title") or "").strip()
        found = re.search(r"/jobs/(\d+)", link)
        if not found or not title:
            raise ScraperError("Teamtailor job missing link or title")
        places = [join_nonempty(loc.findtext(f"{TT}city") or loc.findtext(f"{TT}name"), loc.findtext(f"{TT}country"))
                  for loc in item.iter(f"{TT}location")]
        places = [p for p in places if p]
        remote = (item.findtext("remoteStatus") or "").strip().lower()
        location = "; ".join(places) or None
        if remote in ("fully", "remote"):
            location = f"{location} (Remote)" if location else "Remote"
        job = self.make_job(
            job_id=found[1], title=title, url=link, location=location,
            department=(item.findtext(f"{TT}department") or "").strip() or None,
            date_posted=_rss_date(item.findtext("pubDate")),
        )
        if item.findtext("description"):
            job.extra["description"] = item.findtext("description")
        return job


def _rss_date(value: str | None) -> date | None:
    """An RSS pubDate ("Wed, 23 Sep 2026 15:47:44 +0200") as its date."""
    try:
        return parsedate_to_datetime(value).date() if value else None
    except (TypeError, ValueError):
        return None
