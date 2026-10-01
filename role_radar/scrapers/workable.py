"""Workable public job boards (apply.workable.com/{account}).

  GET https://apply.workable.com/api/v1/widget/accounts/{account}
      every published job, with its locations, in one response (no descriptions)
  GET https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}
      one job's description and requirements, read only for the experience check

Older boards live at {account}.workable.com; the account is the same.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, join_nonempty, parse_date

API = "https://apply.workable.com/api"
_SHARED_HOSTS = {"apply", "www", "jobs"}  # Workable's own subdomains, not an account's


def account_name(url: str) -> str | None:
    """The account in https://apply.workable.com/{account}/ or https://{account}.workable.com."""
    sub = (urlsplit(url).hostname or "").removesuffix("workable.com").rstrip(".")
    return first_path_segment(url) if not sub or sub in _SHARED_HOSTS else sub


def _location(item: dict[str, Any]) -> str | None:
    places = [loc for loc in item.get("locations") or [] if not loc.get("hidden")] or item.get("locations") or []
    names = [join_nonempty(loc.get("city"), loc.get("region"), loc.get("country")) for loc in places]
    location = "; ".join(dict.fromkeys(n for n in names if n)) or None
    if item.get("telecommuting"):
        location = f"{location} (Remote)" if location else "Remote"
    return location


class WorkableScraper(BaseScraper):
    name = "workable"
    domains = ("workable.com",)

    @property
    def account(self) -> str:
        account = self.options.get("account") or account_name(self.company.url)
        if not account:
            raise ScraperError("could not determine Workable account; set options.account")
        return account

    async def fetch_jobs(self) -> ScrapeResult:
        data = await self.http.get_json(f"{API}/v1/widget/accounts/{self.account}")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ScraperError("unexpected Workable response shape")
        return ScrapeResult([self.parse_job(item) for item in data["jobs"] if item.get("shortcode")])

    def parse_job(self, item: dict[str, Any]) -> JobPosting:
        code = item["shortcode"]
        return self.make_job(
            job_id=code,
            title=(item.get("title") or "").strip(),
            url=item.get("url") or f"https://apply.workable.com/{self.account}/j/{code}/",
            location=_location(item),
            employment_type=item.get("employment_type") or None,
            department=item.get("department") or None,
            date_posted=parse_date(item.get("published_on") or item.get("created_at")),
        )

    description_costs_request = True

    async def fetch_description(self, job: JobPosting) -> str | None:
        data = await self.http.get_json(f"{API}/v2/accounts/{self.account}/jobs/{job.job_id}")
        data = data if isinstance(data, dict) else {}
        return "".join(data.get(key) or "" for key in ("description", "requirements")) or None  # not the benefits
