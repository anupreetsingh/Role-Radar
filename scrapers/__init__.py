"""Scraper registry.

To add an ATS: subclass BaseScraper in a new module, set `name` and `domains`,
and add the class to SCRAPERS below.
"""

from __future__ import annotations

from scrapers.ashby import AshbyScraper
from scrapers.bamboohr import BambooHRScraper
from scrapers.base import BaseScraper, ScrapeResult, ScraperError
from scrapers.generic import GenericScraper
from scrapers.greenhouse import GreenhouseScraper
from scrapers.lever import LeverScraper
from scrapers.workday import WorkdayScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    cls.name: cls
    for cls in (BambooHRScraper, GreenhouseScraper, LeverScraper, AshbyScraper, WorkdayScraper, GenericScraper)
}


def scraper_class_for(url: str, ats: str | None = None) -> type[BaseScraper]:
    """Pick a scraper explicitly by `ats`, else by the careers URL's domain, else generic."""
    if ats:
        try:
            return SCRAPERS[ats.lower()]
        except KeyError:
            raise ValueError(f"Unknown ats {ats!r}; choose from {sorted(SCRAPERS)}") from None
    for cls in SCRAPERS.values():
        if cls.handles_url(url):
            return cls
    return GenericScraper


__all__ = ["SCRAPERS", "BaseScraper", "ScrapeResult", "ScraperError", "scraper_class_for"]
