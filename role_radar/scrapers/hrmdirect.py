"""ClearCompany / HRM Direct's public HTML job table.

These tables sometimes omit closing </a> tags. Parse each table cell instead
of treating all subsequent cells as part of the job title. search=true is
required on boards whose landing page only displays a search form.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from role_radar.scrapers.base import BaseScraper, ScrapeResult, ScraperError, join_nonempty


class _JobsTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self.row: dict[str, str] | None = None
        self.cell: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.row = {"id": attrs["data-req-id"]} if attrs.get("data-req-id") else None
        elif tag == "td" and self.row is not None:
            classes = (attrs.get("class") or "").split()
            self.cell = next((c for c in ("posTitle", "cities", "state", "countries", "department") if c in classes), None)
            self.text = []
        elif tag == "a" and self.row is not None and self.cell == "posTitle":
            self.row["url"] = attrs.get("href") or ""

    def handle_data(self, data):
        if self.cell:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "td":
            if self.row is not None and self.cell:
                self.row[self.cell] = " ".join("".join(self.text).split())
            self.cell = None
        elif tag == "tr":
            if self.row is not None:
                self.rows.append(self.row)
            self.row, self.cell = None, None


class HRMDirectScraper(BaseScraper):
    name = "hrmdirect"
    domains = ("hrmdirect.com",)

    async def fetch_jobs(self) -> ScrapeResult:
        parts = urlsplit(self.company.url)
        query = parse_qs(parts.query)
        query["search"] = ["true"]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))
        page = await self.http.get_text(url)
        parser = _JobsTable()
        parser.feed(page)
        if not parser.rows:
            # A login, migration notice or an unsubmitted search form is not an empty board.
            if any(s in page.lower() for s in ("there are currently no", "no current openings", "no jobs match", "no job openings")):
                return ScrapeResult([])
            raise ScraperError("HRM Direct job table missing; board may have moved")
        jobs = {}
        for row in parser.rows:
            if not row.get("posTitle") or not row.get("url"):
                raise ScraperError("HRM Direct job row missing title or link")
            query = parse_qs(urlsplit(row["url"]).query)
            location_id = query.get("req_loc", [""])[0]
            job_id = row["id"] + (f":{location_id}" if location_id else "")
            params = {"req": row["id"]}
            if location_id:
                params["req_loc"] = location_id
            link = urljoin(url, "job-opening.php?" + urlencode(params))
            jobs[job_id] = self.make_job(
                job_id=job_id, title=row["posTitle"], url=link,
                location=join_nonempty(row.get("cities"), row.get("state"), row.get("countries")),
                department=row.get("department"),
            )
        # HTML tables do not advertise a trustworthy total; avoid inferring removals.
        return ScrapeResult(list(jobs.values()), complete=False)
