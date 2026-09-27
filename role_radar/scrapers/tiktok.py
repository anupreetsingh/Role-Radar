"""TikTok careers (lifeattiktok.com), via the public search API the site's pages call.

Careers URL: https://lifeattiktok.com/search
  POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts
The API filters by city codes only, so a check reads every listing (~4,300 jobs,
~43 requests of ~425 KB with descriptions; only the requirements of US jobs are
kept, for the experience filter) and keeps jobs in
options.countries (default ["United States of America"]; [] keeps all). Other
options: locations (TikTok city codes, e.g. CT_1103355), keyword, max_jobs
(default 6000). Listings have no posting date. Check it less often than the
default (settings.check_interval_by_ats).
"""

from __future__ import annotations

from typing import Any

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError

API = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
PAGE_SIZE = 100
HEADERS = {"Origin": "https://lifeattiktok.com", "Referer": "https://lifeattiktok.com/", "website-path": "tiktok"}


def _place(city: dict[str, Any] | None) -> str | None:
    """"San Jose, California, United States of America" from the nested city → state → country codes."""
    names = []
    while city:
        if city.get("en_name"):
            names.append(city["en_name"])
        city = city.get("parent")
    return ", ".join(names) or None


def _country(city: dict[str, Any] | None) -> str | None:
    while city and city.get("parent"):
        city = city["parent"]
    return (city or {}).get("en_name")


class TikTokScraper(BaseScraper):
    name = "tiktok"
    domains = ("lifeattiktok.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        body: dict[str, Any] = {
            "recruitment_id_list": [], "job_category_id_list": [], "subject_id_list": [],
            "location_code_list": list(self.options.get("locations") or []),
            "keyword": self.options.get("keyword", ""), "limit": PAGE_SIZE,
        }  # fmt: skip
        max_jobs = int(self.options.get("max_jobs", 6000))
        countries = set(self.options.get("countries", ["United States of America"]))
        jobs = {}
        offset = 0
        while offset < max_jobs:
            data = await self.http.post_json(API, {**body, "offset": offset}, headers=HEADERS)
            page = (data or {}).get("data") or {}
            posts, count = page.get("job_post_list"), page.get("count")
            if (data or {}).get("code") != 0 or not isinstance(posts, list) or not isinstance(count, int):
                raise ScraperError("unexpected TikTok careers response")
            for item in posts:
                job_id, title = item.get("id"), (item.get("title") or "").strip()
                if not job_id or not title or (countries and _country(item.get("city_info")) not in countries):
                    continue
                jobs[str(job_id)] = self.make_job(
                    job_id=str(job_id), title=title, url=f"https://lifeattiktok.com/search/{job_id}",
                    location=_place(item.get("city_info")),
                    employment_type=(item.get("recruit_type") or {}).get("en_name"),
                    department=(item.get("job_category") or {}).get("en_name"),
                    extra={"description": item.get("requirement")},
                )  # fmt: skip
            offset += len(posts)
            if not posts or offset >= count:
                return ScrapeResult(list(jobs.values()), complete=offset >= count)
        return ScrapeResult(list(jobs.values()), complete=False)
