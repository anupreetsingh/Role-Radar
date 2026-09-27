"""MathWorks' advertised RSS job feed; no search-page rendering required.

The feed includes descriptions, which are ignored. RSS does not promise a full
snapshot, so absence from the feed never marks a previously seen job removed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty

COUNTRIES = {"US": "United States", "CA": "Canada", "AU": "Australia", "IN": "India",
             "UK": "United Kingdom", "DE": "Germany", "FR": "France", "IT": "Italy",
             "ES": "Spain", "CH": "Switzerland", "NL": "Netherlands", "SE": "Sweden",
             "FI": "Finland", "IE": "Ireland", "JP": "Japan", "CN": "China", "KR": "South Korea"}


class MathWorksScraper(BaseScraper):
    name = "mathworks"
    domains = ("mathworks.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        url = self.options.get("feed_url", "https://www.mathworks.com/company/jobs/opportunities/rss.xml")
        try:
            root = ET.fromstring(await self.http.get_text(url))
        except ET.ParseError as exc:
            raise ScraperError("invalid MathWorks job feed") from exc
        if root.tag != "rss" or root.find("channel") is None:
            raise ScraperError("unexpected MathWorks job feed shape")
        jobs = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title_raw") or item.findtext("title") or "").strip()
            job_id = (item.findtext("id") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not job_id or not link:
                raise ScraperError("MathWorks feed contains a job without an id, title or URL")
            country = (item.findtext("location/country") or "").strip()
            location = join_nonempty(item.findtext("location/city"), item.findtext("location/state"), COUNTRIES.get(country, country))
            jobs.append(self.make_job(
                job_id=job_id, title=title, url=link,
                location=location or item.findtext("location/locationName"),
                department=item.findtext("job_function"),
                # "New Career" / "Experienced" are seniority, not employment types.
                employment_type="Internship" if item.findtext("job_type") == "Internships" else None,
            ))
        return ScrapeResult(jobs, complete=False)
