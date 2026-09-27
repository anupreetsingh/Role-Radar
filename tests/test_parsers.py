import asyncio
from dataclasses import fields
from datetime import date

import httpx
import pytest

from models import JobPosting
from scrapers import SCRAPERS, scraper_class_for
from scrapers.base import ScraperError, parse_date
from storage import SeenJob
from tests.conftest import company, fixture_json, fixture_text, job, make_client


def scrape(ats, url, routes, *, details=(), seen=None, **options):
    """Run a scraper against canned responses keyed by URL path.

    Every requested URL is appended to `seen` when given.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, str):
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(200, json=body)

    async def go():
        async with make_client(handler) as http:
            cfg = company(url=url)
            cfg.options = options
            scraper = SCRAPERS[ats](cfg, http)
            result = await scraper.fetch_jobs()
            for j in result.jobs:
                if j.job_id in details:
                    await scraper.fetch_details(j)
            return result

    return asyncio.run(go())


def test_descriptions_are_never_kept():
    # Lever and Ashby always send descriptions; there's nowhere to put them.
    assert "description" not in {f.name for f in fields(JobPosting)}
    assert "description" not in {f.name for f in fields(SeenJob)}


def test_bamboohr_listing_only():
    seen = []
    routes = {"/careers/list": fixture_json("bamboohr_list.json")}
    result = scrape("bamboohr", "https://contfinco.bamboohr.com/careers", routes, details={"303"}, seen=seen)
    jobs = {j.job_id: j for j in result.jobs}
    assert len(jobs) == 4 and result.complete
    dev = jobs["303"]
    assert dev.title == "Mid/Senior Software Developer (.NET Core / React / AWS)"
    assert dev.url == "https://contfinco.bamboohr.com/careers/303"
    assert dev.location == "Wilmington, Delaware (Hybrid)"
    assert dev.employment_type == "Full-Time"
    assert dev.department == "Tech"
    assert dev.uid == "acme:bamboohr:303"
    assert jobs["350"].location == "United States (Remote)"
    assert seen == ["https://contfinco.bamboohr.com/careers/list"]  # no /detail requests


def test_bamboohr_never_wants_details():
    cfg = company(url="https://contfinco.bamboohr.com/careers", locations=["Remote"], employment_types=["full-time"])
    assert not SCRAPERS["bamboohr"](cfg, None).wants_details(job(location=None, employment_type=None))


def test_bamboohr_malformed_response():
    with pytest.raises(ScraperError):
        scrape("bamboohr", "https://x.bamboohr.com/careers", {"/careers/list": {"error": "nope"}})


def test_greenhouse():
    seen = []
    routes = {"/v1/boards/acme/jobs": fixture_json("greenhouse_jobs.json")}
    result = scrape("greenhouse", "https://job-boards.greenhouse.io/acme", routes, seen=seen)
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]  # no ?content=true
    be = result.jobs[0]
    assert (be.job_id, be.title, be.location) == ("111", "Backend Engineer", "Austin, TX")
    assert be.employment_type == "Full-time"
    assert be.department is None  # only sent with content=true
    assert be.date_posted == date(2026, 9, 19)


def test_lever():
    result = scrape("lever", "https://jobs.lever.co/acme", {"/v0/postings/acme": fixture_json("lever_postings.json")})
    (ml,) = result.jobs
    assert ml.title == "Machine Learning Engineer"
    assert ml.location == "New York, NY; Remote - US"
    assert ml.employment_type == "Full-time"
    assert ml.department == "ML"


def test_ashby_skips_unlisted():
    result = scrape("ashby", "https://jobs.ashbyhq.com/acme", {"/posting-api/job-board/acme": fixture_json("ashby_board.json")})
    (ai,) = result.jobs
    assert ai.title == "AI Engineer" and ai.employment_type == "Full Time"
    assert ai.location == "San Francisco; Remote - US"  # already says remote; no suffix
    assert ai.date_posted == date(2026, 9, 24)


def test_workday():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    result = scrape("workday", url, {"/wday/cxs/acme/External/jobs": fixture_json("workday_jobs.json")})
    assert [j.job_id for j in result.jobs] == ["JR2000001", "JR2000002", "JR2000003"]
    assert result.jobs[0].url == "https://acme.wd5.myworkdayjobs.com/External/job/US-TX-Austin/Software-Engineer--New-Grad_JR2000001"
    assert result.jobs[2].location == "2 Locations"
    assert result.complete


def test_workday_detail_fills_locations_and_type_but_not_description():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    routes = {
        "/wday/cxs/acme/External/jobs": fixture_json("workday_jobs.json"),
        "/wday/cxs/acme/External/job/US-TX-Austin/Data-Engineer_JR2000003": fixture_json("workday_detail.json"),
    }
    result = scrape("workday", url, routes, details={"JR2000003"})
    de = result.jobs[2]
    assert de.location == "US, TX, Austin; US, NY, New York"
    assert de.employment_type == "Full time" and de.date_posted == date(2026, 9, 20)


@pytest.mark.parametrize(
    "filters, wanted",
    [
        ({}, set()),  # title-only filter: never
        ({"locations": ["New York"]}, {"JR2000003"}),  # only the "2 Locations" job
        ({"exclude_keywords": ["India"], "exclude_on": ["title", "location"]}, {"JR2000003"}),
        ({"employment_types": ["full time"]}, {"JR2000001", "JR2000002", "JR2000003"}),
    ],
)
def test_workday_wants_details_only_when_filter_needs_them(filters, wanted):
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    listing = scrape("workday", url, {"/wday/cxs/acme/External/jobs": fixture_json("workday_jobs.json")})
    scraper = SCRAPERS["workday"](company(url=url, include_keywords=["engineer"], **filters), None)
    assert {j.job_id for j in listing.jobs if scraper.wants_details(j)} == wanted


def test_generic_json_ld():
    result = scrape("generic", "https://acme.example/careers", {"/careers": fixture_text("generic_jsonld.html")})
    (pe,) = result.jobs
    assert (pe.job_id, pe.title) == ("PE-42", "Platform Engineer")
    assert pe.url == "https://acme.example/careers/platform-engineer"
    assert pe.location == "Denver, CO, US" and pe.employment_type == "Full Time"
    assert pe.date_posted == date(2026, 9, 22)


def test_generic_link_heuristic():
    result = scrape("generic", "https://acme.example/careers", {"/careers": fixture_text("generic_links.html")})
    assert sorted(j.title for j in result.jobs) == ["Data Engineer", "Software Engineer I"]
    assert not result.complete  # heuristic results never imply removals


def test_generic_skips_job_page_unless_filter_needs_a_missing_field():
    listed = job("Data Engineer", job_id=None, location=None, url="https://acme.example/careers/jobs/102")
    plain = SCRAPERS["generic"](company(include_keywords=["engineer"]), None)
    assert not plain.wants_details(listed)  # title and URL are enough
    located = SCRAPERS["generic"](company(include_keywords=["engineer"], locations=["Remote"]), None)
    assert located.wants_details(listed)
    listed.location = "Remote"
    assert not located.wants_details(listed)


def test_generic_delegates_to_embedded_greenhouse():
    routes = {"/careers": fixture_text("generic_embedded.html"), "/v1/boards/acme/jobs": fixture_json("greenhouse_jobs.json")}
    result = scrape("generic", "https://acme.example/careers", routes)
    assert {j.source for j in result.jobs} == {"greenhouse"}


def test_generic_empty_page_raises():
    with pytest.raises(ScraperError):
        scrape("generic", "https://acme.example/careers", {"/careers": "<html><body>Loading…</body></html>"})


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://contfinco.bamboohr.com/careers", "bamboohr"),
        ("https://boards.greenhouse.io/acme", "greenhouse"),
        ("https://jobs.eu.lever.co/acme", "lever"),
        ("https://jobs.ashbyhq.com/acme", "ashby"),
        ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "workday"),
        ("https://www.acme.com/careers", "generic"),
    ],
)
def test_ats_autodetection(url, expected):
    assert scraper_class_for(url).name == expected


def test_parse_date_formats():
    today = date(2026, 9, 25)
    assert parse_date("2026-09-19T09:00:00-04:00") == date(2026, 9, 19)
    assert parse_date(1790000000000) == date(2026, 9, 21)
    assert parse_date("Posted Today", today) == today
    assert parse_date("Posted Yesterday", today) == date(2026, 9, 24)
    assert parse_date("Posted 3 Days Ago", today) == date(2026, 9, 22)
    assert parse_date("Posted 30+ Days Ago", today) == date(2026, 8, 26)
    assert parse_date("sometime") is None
