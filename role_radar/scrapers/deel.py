"""Deel job boards (jobs.deel.com/{org}, e.g. Klarna).

The board page is a Next.js page whose streamed data (self.__next_f) holds every open job as
`"jobPostings":[...]`: title, places, team, employment type and a reference to its
description, which is a text row of the same data. robots.txt disallows /api/, so the page
itself is read: one request a check, descriptions included. Places are the names the
company gave them ("Stockholm", "New York"), without a country.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from role_radar.models import JobPosting
from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, first_path_segment, parse_date

_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
_WORK = {"REMOTE": "Remote", "HYBRID": "Hybrid"}


class DeelScraper(BaseScraper):
    name = "deel"
    domains = ("jobs.deel.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        org = self.options.get("org") or first_path_segment(self.company.url)
        if not org:
            raise ScraperError("could not determine the Deel board; set options.org")
        parts = urlsplit(self.company.url)
        data = "".join(json.loads(f'"{chunk}"') for chunk in _CHUNK.findall(await self.http.get_text(f"{parts.scheme}://{parts.netloc}/{org}")))
        at = data.find('"jobPostings":')
        if at < 0:
            raise ScraperError("Deel board page lists no jobs")
        postings, _ = json.JSONDecoder().raw_decode(data, at + len('"jobPostings":'))
        if not isinstance(postings, list):
            raise ScraperError("unexpected Deel job list")
        origin = f"{parts.scheme}://{parts.netloc}/{org}"
        return ScrapeResult([self.parse_job(item, origin, data) for item in postings])

    def parse_job(self, item: dict[str, Any], origin: str, data: str) -> JobPosting:
        job_id, title = str(item.get("id") or ""), (item.get("title") or "").strip()
        if not job_id or not title:
            raise ScraperError("Deel job missing id or title")
        about = item.get("job") or {}
        places = "; ".join(p["location"]["name"] for p in about.get("jobLocations") or [] if (p.get("location") or {}).get("name"))
        work = _WORK.get(about.get("workArrangementEnum") or "")
        job = self.make_job(
            job_id=job_id, title=title, url=f"{origin}/job-details/{job_id}/overview",
            location=(f"{places} ({work})" if places and work else places or work) or None,
            department=", ".join(d["department"]["name"] for d in about.get("jobDepartments") or [] if (d.get("department") or {}).get("name")) or None,
            employment_type=", ".join(e["employmentType"]["name"] for e in about.get("jobEmploymentTypes") or [] if (e.get("employmentType") or {}).get("name")) or None,
            date_posted=parse_date(item.get("createdAt")),
        )
        description = _text_row(data, item.get("richtextDescription"))
        if description:
            job.extra["description"] = description
        return job


def _text_row(data: str, ref: Any) -> str | None:
    """The text a "$1a" reference points to: the data's row `1a:T{hex byte length},{text}`."""
    if not isinstance(ref, str) or not ref.startswith("$"):
        return ref or None
    # A text row isn't ended by a newline (its length says where it ends), so the next row's ID
    # follows the previous text directly: "...</p>1b:Teb5,<p>...".
    row = re.search(rf"(?<![0-9a-z]){re.escape(ref[1:])}:T([0-9a-f]+),", data)
    if not row:
        return None
    return data[row.end():].encode("utf-8")[: int(row[1], 16)].decode("utf-8", "ignore")
