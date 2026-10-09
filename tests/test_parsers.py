import asyncio
import json
from dataclasses import fields
from datetime import date

import httpx
import pytest

from role_radar.models import JobPosting, html_to_text
from role_radar.scrapers import SCRAPERS, scraper_class_for
from role_radar.scrapers.base import ScraperError, parse_date
from role_radar.storage import SeenJob
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


def test_greenhouse_work_mode_location_takes_the_metadata_places():
    # Cloudflare: the location only says how people work; its metadata names the places.
    jobs = {"jobs": [
        {"id": 1, "title": "Systems Engineer", "location": {"name": "Hybrid"},
         "metadata": [{"name": "Cost Center", "value": "5150"}, {"name": "Job Posting Location", "value": ["Austin, US", "London, UK"]}]},
        {"id": 2, "title": "Software Engineer", "location": {"name": "Distributed; Hybrid"},
         "metadata": [{"name": "Job Posting Location", "value": ["Bengaluru, India"]}]},
        {"id": 3, "title": "Data Engineer", "location": {"name": "Hybrid"}, "metadata": []},
        {"id": 4, "title": "Backend Engineer", "location": {"name": "Remote - US"},
         "metadata": [{"name": "Job Posting Location", "value": ["Austin, US"]}]},
    ]}
    result = scrape("greenhouse", "https://job-boards.greenhouse.io/cloudflare", {"/v1/boards/cloudflare/jobs": jobs})
    assert [j.location for j in result.jobs] == [
        "Austin, US; London, UK (Hybrid)", "Bengaluru, India (Distributed; Hybrid)", "Hybrid", "Remote - US"]


def phenom_job(n, **kw):
    return {"jobSeqNo": f"CHINUSR{n}EXTERNALENUS", "jobId": f"R{n}", "title": f"Software Engineer {n}",
            "cityStateCountry": "Plantation, Florida, United States of America", "multi_location": ["Plantation, FL"],
            "category": "Technology", "type": "Full time", "postedDate": "2026-09-04T00:00:00.000+0000", **kw}


def test_phenom_reads_the_widgets_feed_100_at_a_time():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        start = body["from"]
        jobs = [phenom_job(n) for n in range(start, min(start + 100, 150))]
        if start == 100:
            jobs[0] = phenom_job(100, multi_location=["Plantation, FL", "Seattle, WA"])
        return httpx.Response(200, json={"refineSearch": {"totalHits": 150, "data": {"jobs": jobs}}})

    async def go():
        async with make_client(handler) as http:
            cfg = company("Chewy", url="https://careers.chewy.com/us/en/search-results")
            cfg.options = {"selected_fields": {"country": ["United States of America"]}}
            return await SCRAPERS["phenom"](cfg, http).fetch_jobs()

    result = asyncio.run(go())
    assert result.complete and len(result.jobs) == 150
    assert [(b["from"], b["size"], b["lang"], b["country"]) for b in bodies] == [(0, 100, "en_us", "us"), (100, 100, "en_us", "us")]
    assert bodies[0]["selected_fields"] == {"country": ["United States of America"]}
    first = result.jobs[0]
    assert (first.job_id, first.title, first.department, first.date_posted) == ("CHINUSR0EXTERNALENUS", "Software Engineer 0", "Technology", date(2026, 9, 4))
    assert first.url == "https://careers.chewy.com/us/en/job/R0"
    assert first.location == "Plantation, Florida, United States of America"
    assert next(j for j in result.jobs if j.job_id == "CHINUSR100EXTERNALENUS").location == "Plantation, FL; Seattle, WA"


def test_phenom_global_site_and_cap():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body["lang"], body["country"], body["from"]))
        jobs = [phenom_job(body["from"] + i) for i in range(100)]
        return httpx.Response(200, json={"refineSearch": {"totalHits": 9904, "data": {"jobs": jobs}}})

    async def go():
        async with make_client(handler) as http:
            cfg = company("DHL", url="https://careers.dhl.com/global/en/search-results")
            cfg.options = {"max_jobs": 200}
            return await SCRAPERS["phenom"](cfg, http).fetch_jobs()

    result = asyncio.run(go())
    assert seen == [("https://careers.dhl.com/widgets", "en_global", "global", 0), ("https://careers.dhl.com/widgets", "en_global", "global", 100)]
    assert not result.complete and len(result.jobs) == 200  # capped: never a removal snapshot
    assert result.jobs[0].url == "https://careers.dhl.com/global/en/job/R0"


def test_phenom_description_from_the_job_page():
    page = ('<script>phApp.ddo = {"jobDetail":{"data":{"job":{"description":"<p>3+ years of experience</p>"}}}}; '
            'phApp.experimental = {};</script>')

    async def go():
        async with make_client(lambda r: httpx.Response(200, text=page)) as http:
            scraper = SCRAPERS["phenom"](company("Chewy", url="https://careers.chewy.com/us/en/search-results"), http)
            return await scraper.fetch_description(job(url="https://careers.chewy.com/us/en/job/R1"))

    assert asyncio.run(go()) == "<p>3+ years of experience</p>"


def test_phenom_bad_response_is_error():
    with pytest.raises(ScraperError):
        scrape("phenom", "https://careers.chewy.com/us/en/search-results", {"/widgets": {"status": "error"}})


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


def scrape_rippling(url, *, details=(), described=(), max_pages=None):
    pages, detail = fixture_json("rippling_jobs.json"), fixture_json("rippling_detail.json")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/board/acme/jobs":
            return httpx.Response(200, json=pages[request.url.params["page"]])
        if request.url.path == "/api/v2/board/acme/jobs/a1":
            return httpx.Response(200, json=detail)
        return httpx.Response(404)

    async def go():
        async with make_client(handler) as http:
            cfg = company(url=url)
            cfg.options = {"max_pages": max_pages} if max_pages else {}
            scraper = SCRAPERS["rippling"](cfg, http)
            result = await scraper.fetch_jobs()
            for j in result.jobs:
                if j.job_id in details:
                    await scraper.fetch_details(j)
            descriptions = {j.job_id: await scraper.fetch_description(j) for j in result.jobs if j.job_id in described}
            return result, descriptions, seen

    return asyncio.run(go())


def test_rippling_merges_locations_and_reads_every_page():
    result, _, seen = scrape_rippling("https://ats.rippling.com/en-GB/acme/jobs")  # locale prefix is skipped
    jobs = {j.job_id: j for j in result.jobs}
    assert list(jobs) == ["a1", "b2", "c3"] and result.complete
    grad = jobs["a1"]
    assert grad.title == "Software Engineer, New Grad" and grad.department == "Engineering"
    assert grad.location == "Denver, CO, United States; Remote, United States"
    assert grad.url == "https://ats.rippling.com/acme/jobs/a1" and grad.uid == "acme:rippling:a1"
    assert jobs["c3"].location == "Latin America, Brazil (Remote)"
    assert [u.split("?")[1] for u in seen] == ["page=0&pageSize=1000", "page=1&pageSize=1000"]


def test_rippling_page_cap_leaves_listing_incomplete():
    result, _, _ = scrape_rippling("https://ats.rippling.com/acme/jobs", max_pages=1)
    assert not result.complete and {j.job_id for j in result.jobs} == {"a1", "b2"}


def test_rippling_detail_gives_type_date_and_role_description():
    result, descriptions, seen = scrape_rippling("https://ats.rippling.com/acme/jobs", details={"a1"}, described={"a1"})
    grad = result.jobs[0]
    assert grad.employment_type == "Salaried, full-time" and grad.date_posted == date(2026, 9, 20)
    assert descriptions == {"a1": "<p>Requirements: 0-2 years of experience with Python.</p>"}  # not the company blurb
    assert sum("/jobs/a1" in u for u in seen) == 1  # the description reuses the detail already read


def test_rippling_fetches_description_when_details_were_not_needed():
    _, descriptions, seen = scrape_rippling("https://ats.rippling.com/acme/jobs", described={"a1"})
    assert descriptions["a1"].startswith("<p>Requirements") and sum("/jobs/a1" in u for u in seen) == 1


def scrape_smartrecruiters(*, groups=None, more=None, described=(), **options):
    """`groups` and `more` map a page number to its HTML; a page not in them is empty.
    By default there are two pages of groups, and Bengaluru's "Show more jobs" has one page."""
    if groups is None:
        groups = {"0": fixture_text("smartrecruiters_groups0.html"), "1": fixture_text("smartrecruiters_groups1.html")}
    more = {"1": fixture_text("smartrecruiters_more1.html")} if more is None else more
    pages = {"/Acme/api/groups": groups, "/Acme/api/more": more}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "jobs.smartrecruiters.com":
            return httpx.Response(200, json=fixture_json("smartrecruiters_job.json"))
        if request.url.path not in pages:
            return httpx.Response(404)
        return httpx.Response(200, text=pages[request.url.path].get(request.url.params["page"], ""))

    async def go():
        async with make_client(handler) as http:
            cfg = company(url="https://careers.smartrecruiters.com/Acme")
            cfg.options = options
            scraper = SCRAPERS["smartrecruiters"](cfg, http)
            result = await scraper.fetch_jobs()
            descriptions = {j.job_id: await scraper.fetch_description(j) for j in result.jobs if j.job_id in described}
            return result, descriptions, seen

    return asyncio.run(go())


def test_smartrecruiters_reads_every_group_and_its_more_pages():
    result, _, seen = scrape_smartrecruiters()
    jobs = {j.job_id: j for j in result.jobs}
    assert result.complete and len(jobs) == 6
    swe = jobs["744000000000101"]
    assert swe.title == "Software Engineer I" and swe.employment_type == "Full-time"
    assert swe.url == "https://jobs.smartrecruiters.com/Acme/744000000000101-software-engineer-i"
    assert swe.uid == "acme:smartrecruiters:744000000000101"
    assert swe.location == "Austin, TX; Sydney, Australia"  # listed in both groups
    assert jobs["744000000000102"].location == "Austin, TX (Remote)" and jobs["744000000000102"].employment_type == "Contract"
    assert jobs["744000000000301"].title == "Site Reliability Engineer & SRE Lead"
    assert {j.location for i, j in jobs.items() if i.startswith("7440000000002")} == {"Bengaluru, India", "Bengaluru, India (Remote)"}
    assert [u.split(".com", 1)[1] for u in seen] == [
        "/Acme/api/groups?page=0",
        "/Acme/api/more?type=location&value=Bengaluru%2C%20IN&page=1",
        "/Acme/api/more?type=location&value=Bengaluru%2C%20IN&page=2",
        "/Acme/api/groups?page=1",
        "/Acme/api/groups?page=2",
    ]


def test_smartrecruiters_listing_is_incomplete_when_cut_short():
    short_group, _, _ = scrape_smartrecruiters(more={})  # "Show more jobs" gives nothing: 1 of Bengaluru's 3 jobs
    assert not short_group.complete and len(short_group.jobs) == 4
    page_cap, _, seen = scrape_smartrecruiters(max_pages=2)  # the empty page after the last wasn't reached
    assert not page_cap.complete and len(page_cap.jobs) == 6 and "page=2" not in seen[-1]


def test_smartrecruiters_stops_on_a_repeated_more_page():
    repeat = fixture_text("smartrecruiters_more1.html")
    result, _, seen = scrape_smartrecruiters(more={"1": repeat, "2": repeat, "3": repeat})
    assert result.complete and sum("/api/more" in u for u in seen) == 2


def test_smartrecruiters_empty_and_unrecognized_pages():
    empty, _, _ = scrape_smartrecruiters(groups={})
    assert empty.jobs == [] and empty.complete
    with pytest.raises(ScraperError, match="missing"):
        scrape_smartrecruiters(groups={"0": "<html><body>Something went wrong</body></html>"})
    by_department = fixture_text("smartrecruiters_groups0.html").replace('data-type="location"', 'data-type="department"')
    with pytest.raises(ScraperError, match="department"):
        scrape_smartrecruiters(groups={"0": by_department})


def test_smartrecruiters_description_is_the_role_not_the_company():
    result, descriptions, seen = scrape_smartrecruiters(described={"744000000000101"})
    assert descriptions == {"744000000000101": "<p>Build our booking APIs.</p><ul><li>0-2 years of experience with Python</li></ul>"}
    swe = next(j for j in result.jobs if j.job_id == "744000000000101")
    assert swe.date_posted == date(2026, 9, 21)
    assert seen[-1] == swe.url


def scrape_workable(url="https://apply.workable.com/acme/", *, described=(), **options):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v1/widget/accounts/acme":
            return httpx.Response(200, json=fixture_json("workable_widget.json"))
        if request.url.path == "/api/v2/accounts/acme/jobs/D1E02E0D44":
            return httpx.Response(200, json=fixture_json("workable_job.json"))
        return httpx.Response(404)

    async def go():
        async with make_client(handler) as http:
            cfg = company(url=url)
            cfg.options = options
            scraper = SCRAPERS["workable"](cfg, http)
            result = await scraper.fetch_jobs()
            descriptions = {j.job_id: await scraper.fetch_description(j) for j in result.jobs if j.job_id in described}
            return result, descriptions, seen

    return asyncio.run(go())


def test_workable_widget_lists_every_job_in_one_request():
    result, _, seen = scrape_workable()
    jobs = {j.job_id: j for j in result.jobs}
    assert result.complete and list(jobs) == ["D1E02E0D44", "7414178934", "76675200E4"] and len(seen) == 1
    swe = jobs["D1E02E0D44"]
    assert swe.title == "Software Engineer I" and swe.uid == "acme:workable:D1E02E0D44"
    assert swe.url == "https://apply.workable.com/j/D1E02E0D44"
    assert swe.location == "Noida, Uttar Pradesh, India; New York, United States"  # the hidden Pune location left out
    assert (swe.employment_type, swe.department, swe.date_posted) == ("Full-time", "Engineering", date(2026, 9, 25))
    director = jobs["7414178934"]
    assert director.location == "United States (Remote)" and director.employment_type is None and director.department is None
    se = jobs["76675200E4"]
    assert se.location == "Remote" and se.date_posted == date(2026, 8, 27)
    assert se.url == "https://apply.workable.com/acme/j/76675200E4/"


@pytest.mark.parametrize("url", ["https://apply.workable.com/acme/", "https://acme.workable.com/", "https://apply.workable.com/acme/j/D1E02E0D44/"])
def test_workable_account_from_url(url):
    result, _, _ = scrape_workable(url)
    assert len(result.jobs) == 3


def test_workable_description_is_the_role_not_the_benefits():
    _, descriptions, seen = scrape_workable(described={"D1E02E0D44"})
    assert descriptions == {"D1E02E0D44": "<p>Build our booking APIs.</p><ul><li>0-2 years of experience with Python</li></ul>"}
    assert seen[-1] == "https://apply.workable.com/api/v2/accounts/acme/jobs/D1E02E0D44"


def scrape_successfactors(sitemap, *, search=None, known=None, details=(), described=(), **options):
    """Run the SuccessFactors reader on jobs.acme.example; search maps startrow → page (default: no rows)."""
    seen = []
    search = {"0": "<html><body>Loading…</body></html>"} if search is None else search

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if request.url.path == "/search/":
            return httpx.Response(200, text=search.get(request.url.params["startrow"], "<table></table>"))
        if "/job/" in request.url.path:
            return httpx.Response(200, text=fixture_text("successfactors_job.html"))
        return httpx.Response(404)

    async def go():
        async with make_client(handler) as http:
            cfg = company(url="https://jobs.acme.example/")
            cfg.options = options
            scraper = SCRAPERS["successfactors"](cfg, http)
            scraper.known = known or {}
            result = await scraper.fetch_jobs()
            wanted = {j.job_id for j in result.jobs if scraper.wants_details(j)}
            for j in result.jobs:
                if j.job_id in details:
                    await scraper.fetch_details(j)
            descriptions = {j.job_id: await scraper.fetch_description(j) for j in result.jobs if j.job_id in described}
            return result, wanted, descriptions, seen

    return asyncio.run(go())


def test_successfactors_job_feed_lists_every_job_with_its_description():
    result, wanted, descriptions, seen = scrape_successfactors(fixture_text("successfactors_feed.xml"), described={"1436476000"})
    jobs = {j.job_id: j for j in result.jobs}
    assert result.complete and list(jobs) == ["1436476000", "1434335900"] and not wanted
    ai = jobs["1436476000"]
    assert (ai.title, ai.location, ai.department) == ("Engineer, IT AI", "Fort Worth, TX, US", "Information Technology")
    assert ai.url == "https://jobs.aa.com/job/Fort-Worth-Engineer%2C-IT-AI-TX-76101/1436476000/"
    assert ai.uid == "acme:successfactors:1436476000"
    assert jobs["1434335900"].title == "Cleaner & Porter"
    assert "2 years of experience" in html_to_text(descriptions["1436476000"])
    # The feed, then the search page, which this site fills in the browser: nothing more to read.
    assert [u.split(".example", 1)[1] for u in seen] == [
        "/sitemap.xml", "/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=0"]


def test_successfactors_links_take_titles_from_the_search_pages():
    page = fixture_text("successfactors_search.html")
    result, wanted, _, seen = scrape_successfactors(fixture_text("successfactors_links.xml"), search={"0": page})
    jobs = {j.job_id: j for j in result.jobs}
    assert list(jobs) == ["1406504600", "1426527300", "1434181900", "1440000001"]
    rebar = jobs["1406504600"]
    assert (rebar.title, rebar.location, rebar.date_posted) == ("Rebar Project Manager", "Lexington, NC, US, 27292", date(2026, 10, 2))
    assert rebar.url == "https://jobs.acme.example/job/Lexington-Rebar-Project-Manager-NC-27292/1406504600/"
    swe = jobs["1440000001"]  # on the search page, not yet in the sitemap
    assert swe.title == "Software Engineer, Data & AI" and swe.location == "Austin, TX, US, 73301"
    assert not result.complete  # the sitemap is behind the search: a job missing from it may still be open
    # Jobs on no page read keep their links' words; the first check's jobs aren't read one by one.
    assert jobs["1426527300"].title == "Brandenburg Automation Controls Engineering Intern Summer 2027 KY 40108"
    assert jobs["1426527300"].location is None and not wanted
    # Untitled jobs remained, so the next page was read; it was empty, so reading stopped.
    assert [u.rsplit("=", 1)[1] for u in seen if "/search/" in u] == ["0", "2"]


def test_successfactors_reuses_known_jobs_and_reads_new_ones_page_when_they_could_match():
    links = fixture_text("successfactors_links.xml")
    first, _, _, _ = scrape_successfactors(links)
    known = {j.uid: SeenJob(title=j.title, url=j.url, fingerprint=j.fingerprint, first_seen="x", location="Kept, KY")
             for j in first.jobs if j.job_id != "1426527300"}
    result, wanted, descriptions, seen = scrape_successfactors(links, known=known, details={"1426527300"},
                                                               described={"1426527300"})
    jobs = {j.job_id: j for j in result.jobs}
    assert result.complete and jobs["1406504600"].location == "Kept, KY"  # stored title and location
    assert wanted == {"1426527300"}  # the one new job
    intern = jobs["1426527300"]
    assert intern.title == "Automation & Controls Engineering Intern- Summer 2027"
    assert intern.location == "Brandenburg, KY, US" and intern.date_posted == date(2026, 10, 2)
    assert descriptions["1426527300"] == "Pursuing a degree in electrical engineering.\nNo experience required."
    assert sum("/job/" in u for u in seen) == 1  # the description came with the details


def test_successfactors_rejects_a_page_that_is_not_a_sitemap():
    with pytest.raises(ScraperError, match="sitemap"):
        scrape_successfactors("<html><body>Access Denied</body></html>")


def test_successfactors_link_words_and_place():
    from role_radar.scrapers.successfactors import link_location, link_words

    assert link_words("Fort-Worth-Engineer%2C-IT-AI-TX-76101") == "Fort Worth Engineer IT AI TX 76101"
    assert link_location("Fort-Worth-Engineer%2C-IT-AI-TX-76101", "Engineer, IT AI") == "Fort Worth, TX, 76101"
    assert link_location("Pune-SOLUTION-ARCHITECT-L1%28CONTRACT%29-IND-411005", "Solution Architect L1(Contract)") == "Pune, IND, 411005"
    assert link_location("Austin-Data-Analyst-TX-73301", "Software Engineer") is None


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
        ("https://ats.rippling.com/en-US/acme/jobs", "rippling"),
        ("https://careers.smartrecruiters.com/Acme", "smartrecruiters"),
        ("https://apply.workable.com/acme/", "workable"),
        ("https://assaabloy.jobs2web.com/", "successfactors"),
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


def test_gem():
    posts = [
        {"id": "4003629005", "title": "Software Engineer", "absolute_url": "https://jobs.gem.com/retool/4003629005",
         "location": {"name": "San Francisco, United States"}, "offices": [{"location": {"name": "San Francisco, United States"}}],
         "departments": [{"name": "Engineering"}], "employment_type": "full_time", "first_published_at": "2026-09-25T18:46:39.000Z",
         "content": "<p>3+ years experience</p>"},
        {"id": "2", "title": "Account Executive", "absolute_url": "https://jobs.gem.com/retool/2", "location": {"name": "New York"},
         "offices": [{"location": {"name": "New York, United States"}}, {"location": {"name": "London, United Kingdom"}}]},
    ]
    result = scrape("gem", "https://jobs.gem.com/retool", {"/job_board/v0/retool/job_posts": posts})
    eng, ae = result.jobs
    assert (eng.job_id, eng.title, eng.url) == ("4003629005", "Software Engineer", "https://jobs.gem.com/retool/4003629005")
    assert (eng.location, eng.department, eng.employment_type, eng.date_posted) == ("San Francisco, United States", "Engineering", "full-time", date(2026, 9, 25))
    assert eng.extra["description"] == "<p>3+ years experience</p>"
    assert ae.location == "New York, United States; London, United Kingdom"
    assert scraper_class_for("https://jobs.gem.com/retool").name == "gem"
