"""Scraper registry.

To add an ATS: subclass BaseScraper in a new module, set `name` and `domains`,
and add the class to SCRAPERS below.
"""

from __future__ import annotations

from role_radar.scrapers.amazon import AmazonScraper
from role_radar.scrapers.apple import AppleScraper
from role_radar.scrapers.ashby import AshbyScraper
from role_radar.scrapers.avature import AvatureScraper
from role_radar.scrapers.bamboohr import BambooHRScraper
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError
from role_radar.scrapers.comsol import ComsolScraper
from role_radar.scrapers.deshaw import DEShawScraper
from role_radar.scrapers.eightfold import EightfoldScraper
from role_radar.scrapers.gem import GemScraper
from role_radar.scrapers.generic import GenericScraper
from role_radar.scrapers.google import GoogleScraper
from role_radar.scrapers.greenhouse import GreenhouseScraper
from role_radar.scrapers.hrmdirect import HRMDirectScraper
from role_radar.scrapers.icims import ICIMSScraper
from role_radar.scrapers.jibe import JibeScraper
from role_radar.scrapers.lever import LeverScraper
from role_radar.scrapers.mathworks import MathWorksScraper
from role_radar.scrapers.meta import MetaScraper
from role_radar.scrapers.oracle_hcm import OracleHcmScraper
from role_radar.scrapers.phenom import PhenomScraper
from role_radar.scrapers.recruiterbox import RecruiterboxScraper
from role_radar.scrapers.rippling import RipplingScraper
from role_radar.scrapers.smartrecruiters import SmartRecruitersScraper
from role_radar.scrapers.successfactors import SuccessFactorsScraper
from role_radar.scrapers.tiktok import TikTokScraper
from role_radar.scrapers.workable import WorkableScraper
from role_radar.scrapers.workday import WorkdayScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    cls.name: cls
    for cls in (
        BambooHRScraper, GreenhouseScraper, LeverScraper, AshbyScraper, WorkdayScraper,
        MathWorksScraper, HRMDirectScraper, ICIMSScraper, JibeScraper, AvatureScraper,
        ComsolScraper, RecruiterboxScraper, AmazonScraper, AppleScraper, GoogleScraper, EightfoldScraper,
        OracleHcmScraper, MetaScraper, TikTokScraper, RipplingScraper, SmartRecruitersScraper, WorkableScraper,
        SuccessFactorsScraper, PhenomScraper, DEShawScraper, GemScraper,
        GenericScraper,
    )
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


def ats_name(url: str, ats: str | None = None) -> str | None:
    """The scraper name a company resolves to (e.g. "workday"), or None if `ats` is unknown."""
    try:
        return scraper_class_for(url, ats).name
    except ValueError:
        return None


__all__ = ["SCRAPERS", "BaseScraper", "ScrapeResult", "ScraperError", "ats_name", "scraper_class_for"]
