"""EasyApply job boards ({company}.easyapply.co: Heritage Park, Mount Norquay, Canadian Rocky Mountain Resorts...).

  GET https://{company}.easyapply.co/rss   every open job: its title, place, post date and whole description
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, mend_xml, parse_date


class EasyApplyScraper(BaseScraper):
    name = "easyapply"
    domains = ("easyapply.co",)

    @property
    def board(self) -> str:
        host = urlsplit(self.company.url).hostname
        if not host or host in ("easyapply.co", "www.easyapply.co"):
            raise ScraperError("use the company's own board, https://{company}.easyapply.co")
        return f"https://{host}"

    async def fetch_jobs(self) -> ScrapeResult:
        text = await self.http.get_text(f"{self.board}/rss")
        if "<rss" not in text[:1000]:  # a closed board sends its visitors to EasyApply's job-seekers page
            raise ScraperError("no EasyApply board at this address (closed or renamed)")
        try:
            root = ET.fromstring(mend_xml(text))
        except ET.ParseError as exc:
            raise ScraperError("invalid EasyApply job feed") from exc
        if root.find("channel") is None:
            raise ScraperError("unexpected EasyApply job feed shape")
        jobs = []
        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            title = " ".join((item.findtext("title") or "").split())
            if not link or not title:
                continue
            jobs.append(self.make_job(
                job_id=link.rstrip("/").rsplit("/", 1)[-1], title=title, url=link,
                location=(item.findtext("category") or "").strip() or None,
                date_posted=parse_date(item.findtext("pubDate")),
                extra={"description": item.findtext("description")},
            ))
        return ScrapeResult(jobs)
