"""Taleo Business Edition career sites ({dc}.tbe.taleo.net, e.g. Banff Centre's
https://phe.tbe.taleo.net/phe02/ats/careers/v2/searchResults?org=BANFFCENTRE&cws=39).

  GET https://{host}/{instance}/ats/servlet/Rss?org={org}&cws={cws}&WebPage=SRCHR_V2&WebVersion=0&_rss_version=2
      every posted job of that career site: title, place, department, post date and whole description
      (the search page shows ten at a time)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import (
    COUNTRY_NAMES, BaseScraper, ScrapeResult, ScraperError, first_path_segment, join_nonempty, mend_xml, parse_date,
)

_TBE = "{urn:TBERss}"


class TaleoBEScraper(BaseScraper):
    name = "taleo_be"
    domains = ("tbe.taleo.net",)

    @property
    def feed(self) -> str:
        parts = urlsplit(self.company.url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        instance = first_path_segment(self.company.url)
        if not (parts.hostname and instance and query.get("org") and query.get("cws")):
            raise ScraperError("use the career site's address, with its org and cws: .../careers/v2/searchResults?org=...&cws=...")
        params = {"org": query["org"], "cws": query["cws"], "WebPage": "SRCHR_V2", "WebVersion": "0", "_rss_version": "2"}
        return f"https://{parts.hostname}/{instance}/ats/servlet/Rss?{urlencode(params)}"

    async def fetch_jobs(self) -> ScrapeResult:
        text = await self.http.get_text(self.feed)
        if "<rss" not in text[:2000]:  # a closed or employees-only career site sends no feed
            raise ScraperError("no job feed for this career site (closed, or for employees only)")
        try:
            root = ET.fromstring(mend_xml(text))
        except ET.ParseError as exc:
            raise ScraperError("invalid Taleo Business Edition job feed") from exc
        if root.find("channel") is None:
            raise ScraperError("unexpected Taleo Business Edition job feed shape")
        jobs = [self.parse_item(item) for item in root.findall("./channel/item")]
        return ScrapeResult([job for job in jobs if job])

    def parse_item(self, item: ET.Element) -> JobPosting | None:
        job_id = (item.findtext(f"{_TBE}reqId") or "").strip()
        title = " ".join((item.findtext("title") or "").split())
        if not job_id or not title:
            return None
        country = (item.findtext(f"{_TBE}locationCountry") or "").strip()
        place = join_nonempty(item.findtext(f"{_TBE}locationCity"), item.findtext(f"{_TBE}locationState"),
                              COUNTRY_NAMES.get(country, country))
        return self.make_job(
            job_id=job_id, title=title, url=(item.findtext("link") or "").strip() or self.company.url,
            location=place or item.findtext(f"{_TBE}location"),
            department=(item.findtext(f"{_TBE}department") or "").strip() or None,
            date_posted=parse_date(item.findtext("pubDate")),
            extra={"description": item.findtext(f"{_TBE}html-description") or item.findtext("description")},
        )
