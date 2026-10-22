"""Push Operations job boards (restaurants: Cactus Club Cafe, Earls...), by organization: every location's
open jobs in one request.

Careers URL: https://app-elb.pushoperations.com/ats/job-postings/{company uuid} of any of the organization's
locations, with options.organization (the uuid its page names in data-organization-uuid).
  GET https://app-elb.pushoperations.com/api/v2/organizations/{organization}/companies/public
      ?include=jobPostings.position.location
      each location (city, province, country) with its published job postings
"""

from __future__ import annotations

from typing import Any

from role_radar.models import JobPosting
from role_radar.scrapers.base import COUNTRY_NAMES, BaseScraper, ScrapeResult, ScraperError, join_nonempty, parse_date

SITE = "https://app-elb.pushoperations.com"


def _data(value: Any) -> list[dict[str, Any]]:
    """An included relation: {"data": [...]} or the list itself."""
    value = value.get("data") if isinstance(value, dict) else value
    return [v for v in value or [] if isinstance(v, dict)]


class PushScraper(BaseScraper):
    name = "push"
    domains = ("pushoperations.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        organization = self.options.get("organization")
        if not organization:
            raise ScraperError("set options.organization (the data-organization-uuid on a location's job page)")
        data = await self.http.get_json(f"{SITE}/api/v2/organizations/{organization}/companies/public"
                                        "?include=jobPostings.position.location")
        companies = data.get("data") if isinstance(data, dict) else None
        if not isinstance(companies, list):
            raise ScraperError("unexpected Push Operations response shape")
        jobs = [self.parse_job(company, posting) for company in companies for posting in _data(company.get("jobPostings"))
                if posting.get("id")]
        return ScrapeResult([job for job in jobs if job.title])

    def parse_job(self, company: dict[str, Any], posting: dict[str, Any]) -> JobPosting:
        country = company.get("country")
        place = join_nonempty(company.get("city"), company.get("province"), COUNTRY_NAMES.get(country or "", country))
        position = posting.get("position") or {}
        position = position.get("data", position) if isinstance(position, dict) else {}
        title = posting.get("externalPositionName") or position.get("name") or posting.get("description") or ""
        return self.make_job(
            job_id=str(posting["id"]),
            title=" ".join(str(title).split()),
            url=f"{SITE}/ats/job-postings/{company.get('uuid')}",
            location=place or posting.get("region"),
            employment_type=posting.get("employmentType") or None,
            department=company.get("name"),
            date_posted=parse_date(posting.get("createdAt")),
            extra={"description": posting.get("body")},
        )
