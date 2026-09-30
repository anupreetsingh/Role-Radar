"""The experience filter: reading requirements, where descriptions come from, and the monitor step."""

import asyncio
import json

import httpx
import pytest

from role_radar import monitor
from role_radar.config import CompanyConfig, Settings
from role_radar.experience import assess
from role_radar.filters import JobFilter
from role_radar.scrapers import SCRAPERS
from role_radar.storage import MonitorState
from tests.conftest import company, make_client

KEEP, DROP = True, False


@pytest.mark.parametrize(
    "text, keep",
    [
        # plain requirements
        ("3+ years of software engineering experience", DROP),
        ("2+ years of software engineering experience", KEEP),
        ("0-2 years of experience", KEEP),
        ("2-4 years of experience in backend development", KEEP),
        ("3-5 years of experience", DROP),
        ("At least three (3) years of professional experience", DROP),
        ("Minimum of 5 years building distributed systems", DROP),
        ("5+ years of Python", DROP),
        ("8–12+ years technical program management in engineering organizations", DROP),
        ("Years of experience: 4+", DROP),
        ("You have less than two years of engineering experience.", KEEP),
        ("Up to 3 years of experience", KEEP),
        ("Master's degree and 2+ years of Python and 3+ years of AWS", DROP),
        # a master's instead of experience
        ("3+ years of experience or a Master's degree in Computer Science", KEEP),
        ("Bachelor's degree with 4+ years of experience, or a Master's degree with 2+ years of experience", KEEP),
        ("BS + 4 years or MS + 2 years of relevant experience", KEEP),
        ("Bachelor's degree and 3+ years of experience, or Master's degree", KEEP),
        ("Master's degree, or Bachelor's degree with 4+ years of experience", KEEP),
        ("3+ years of experience (2+ with a Master's degree)", KEEP),
        ("4+ years of experience with a BS, 2+ years with an MS", KEEP),
        ("5+ years of experience, or 2+ years with a Master's", KEEP),
        ("2 years of experience with software development in one or more programming languages, "
         "or 1 year of experience with an advanced degree.", KEEP),
        ("Bachelor's Degree in Computer Science or related technical field AND 2+ years technical engineering experience "
         "with coding in languages including, but not limited to, C, C++, Java, or Python OR Master's Degree in Computer "
         "Science or related technical field with 1+ year(s) technical engineering experience OR equivalent experience.", KEEP),
        ("3+ years of experience. A Master's degree may substitute for one year of experience.", KEEP),
        # ...and where the master's path still needs too much
        ("Bachelor's degree + 5 years of experience or Master's degree + 3 years", DROP),
        ("MS in Computer Science and 3+ years of experience", DROP),
        ("BS/MS in CS and 5+ years of experience", DROP),
        ("BS or MS in Computer Science and 5+ years of industry experience", DROP),
        ("Master's degree or PhD in CS, and 4+ years of experience", DROP),
        ("Master's degree in CS or related field and 3+ years of experience", DROP),
        ("5+ years of experience (3+ with a Master's degree)", DROP),
        ("5 years of experience with software development, or 3 years with an advanced degree", DROP),
        ("Bachelor's Degree AND 4+ years technical engineering experience OR Master's Degree AND 3+ years technical "
         "engineering experience OR Doctorate AND 1+ year technical engineering experience", DROP),
        ("5+ years of experience or PhD", DROP),
        ("PhD with 1+ years of experience or Bachelor's with 5+ years", DROP),
        ("Scrum Master certification and 5+ years of experience", DROP),
        ("Experience with MS SQL and 4+ years of backend development", DROP),
        # preferences don't count; the kind of experience wanted does
        ("3+ years of experience preferred", KEEP),
        ("3+ years of relevant experience is ideal.", KEEP),
        ("Ideally 4+ years of experience with Go", KEEP),
        ("Preferred: 3+ years of experience", KEEP),
        ("3 years of experience required; MS preferred", DROP),
        ("5+ years of experience (Master's preferred)", DROP),
        ("5+ years of product management experience, ideally with products where data quality matters", DROP),
        ("3+ years of experience, preferably in fintech", DROP),
        ("The ideal candidate has 5+ years of experience", DROP),
        # sections
        ("Requirements:\n- 2+ years of experience\n\nNice to have:\n- 5+ years of Kubernetes", KEEP),
        ("Preferred Qualifications\n5+ years of experience\nMinimum Qualifications\n1+ years of experience", KEEP),
        ("Minimum Qualifications\n5+ years of experience", DROP),
        ("<h3>Minimum qualifications:</h3><ul><li>Bachelor's degree or equivalent practical experience.</li>"
         "<li>2 years of experience in sales.</li></ul><h3>Preferred qualifications:</h3><ul><li>5 years of experience.</li></ul>",
         KEEP),
        ("- 3+ years of non-internship professional software development experience<br/>- Bachelor's degree", DROP),
        # years that aren't about the candidate
        ("About Us\nWith 10 years of experience serving customers, Acme is a leader.\nRequirements\n1+ year of experience", KEEP),
        ("We have been building AI for 10 years. You have 2+ years of experience.", KEEP),
        ("Our founders have 8 years of experience in fintech.", KEEP),
        ("With 20+ years of experience serving customers, Acme is a leader.", KEEP),
        ("Bachelor's degree (4-year degree) required", KEEP),
        ("Must have graduated within the last 2 years", KEEP),
        ("401(k) match after 1 year of service", KEEP),
        ("Named a best place to work 5 years in a row", KEEP),
        # nothing to go on
        ("", KEEP),
        (None, KEEP),
        ("Great communication skills. Experience with Python.", KEEP),
    ],
)
def test_reads_the_years_a_posting_requires(text, keep):
    assert assess(text, 2).keep is keep


def test_verdict_quotes_the_requirement_it_dropped_on():
    verdict = assess("<ul><li>1+ years of Go</li><li>4+ years of backend engineering experience</li></ul>", 2)
    assert not verdict.keep and verdict.years == 4
    assert verdict.reason == 'needs 4+ years: "4+ years of backend engineering experience"'
    assert assess("2+ years of experience", 3).keep  # the limit is the filter's


def test_filter_setting_is_validated():
    assert JobFilter.from_config({"max_experience_years": 2}).max_experience_years == 2
    assert JobFilter.from_config({"max_experience_years": None}).max_experience_years is None
    for bad in (-1, True, "2"):
        with pytest.raises(ValueError):
            JobFilter(max_experience_years=bad)


# -- where each source's description comes from --------------------------------


def describe(ats, url, routes, **options):
    """The first listed job's description, and every requested path.

    `routes` maps a URL path to a JSON body, or to text for HTML pages.
    """
    requested = []

    def handler(request):
        requested.append(request.url.path)
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)

    async def go():
        async with make_client(handler) as http:
            cfg = company(url=url)
            cfg.options = options
            scraper = SCRAPERS[ats](cfg, http)
            job = (await scraper.fetch_jobs()).jobs[0]
            listed = len(requested)
            return await scraper.fetch_description(job), requested[listed:]

    return asyncio.run(go())


def test_listing_descriptions_need_no_request():
    ashby = {"jobs": [{"id": "a1", "title": "Software Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
                       "descriptionHtml": "<p>2+ years of experience</p>"}]}
    assert describe("ashby", "https://jobs.ashbyhq.com/acme", {"/posting-api/job-board/acme": ashby}) == (
        "<p>2+ years of experience</p>", [])

    lever = [{"id": "l1", "text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/acme/l1",
              "description": "<p>About the team</p>", "lists": [{"text": "Requirements", "content": "<li>3+ years</li>"}],
              "additional": "<p>Benefits</p>"}]
    text, requested = describe("lever", "https://jobs.lever.co/acme", {"/v0/postings/acme": lever})
    assert requested == [] and "<h3>Requirements</h3><ul><li>3+ years</li></ul>" in text and "About the team" in text

    amazon = {"hits": 1, "jobs": [{"id_icims": "9", "title": "SDE", "basic_qualifications": "- 3+ years of experience",
                                   "preferred_qualifications": "- Master's degree"}]}
    assert describe("amazon", "https://www.amazon.jobs/en/search", {"/en/search.json": amazon}) == (
        "- 3+ years of experience", [])

    tiktok = {"code": 0, "data": {"count": 1, "job_post_list": [
        {"id": "7", "title": "Software Engineer", "requirement": "Minimum Qualifications\n- 1+ years",
         "city_info": {"en_name": "San Jose", "parent": {"en_name": "United States of America"}}}]}}
    assert describe("tiktok", "https://lifeattiktok.com/search", {"/api/v1/public/supplier/search/job/posts": tiktok}) == (
        "Minimum Qualifications\n- 1+ years", [])


def test_google_prefers_the_minimum_qualifications():
    row = [None] * 21
    row[0], row[1], row[12] = "1", "Software Engineer", [1790377680, 0]
    row[4] = [None, "<h3>Minimum qualifications:</h3><ul><li>2 years</li></ul><h3>Preferred qualifications:</h3>"]
    row[19] = [None, "<ul><li>2 years of experience</li></ul>"]
    page = (f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{json.dumps([[row], None, 1, 20])}, "
            "sideChannel: {}});</script>")
    text, requested = describe("google", "https://www.google.com/about/careers/applications/jobs/results/",
                               {"/about/careers/applications/jobs/results/": page})
    assert text == "<ul><li>2 years of experience</li></ul>" and requested == []


def test_greenhouse_and_workday_read_the_job_endpoint():
    greenhouse = {"jobs": [{"id": 111, "title": "Backend Engineer", "absolute_url": "https://example.com/111"}]}
    assert describe("greenhouse", "https://job-boards.greenhouse.io/acme", {
        "/v1/boards/acme/jobs": greenhouse, "/v1/boards/acme/jobs/111": {"content": "&lt;p&gt;3+ years&lt;/p&gt;"},
    }) == ("&lt;p&gt;3+ years&lt;/p&gt;", ["/v1/boards/acme/jobs/111"])

    path = "/job/US-TX-Austin/Software-Engineer_JR1"
    workday = {"total": 1, "jobPostings": [{"title": "Software Engineer", "externalPath": path}]}
    assert describe("workday", "https://acme.wd5.myworkdayjobs.com/External", {
        "/wday/cxs/acme/External/jobs": workday,
        f"/wday/cxs/acme/External{path}": {"jobPostingInfo": {"jobDescription": "<p>2+ years</p>"}},
    }) == ("<p>2+ years</p>", [f"/wday/cxs/acme/External{path}"])


def test_workday_reuses_a_detail_already_fetched():
    async def go():
        async with make_client(lambda r: httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>4+ years</p>"}})) as http:
            scraper = SCRAPERS["workday"](company(url="https://acme.wd5.myworkdayjobs.com/External"), http)
            job = scraper.parse_listing({"title": "Software Engineer", "externalPath": "/job/X_JR1"})
            await scraper.fetch_details(job)
            return await scraper.fetch_description(job), http.stats.requests

    assert asyncio.run(go()) == ("<p>4+ years</p>", 1)


def test_oracle_eightfold_and_apple_read_each_jobs_details():
    oracle_list = {"items": [{"TotalJobsCount": 1, "requisitionList": [{"Id": "42", "Title": "Software Engineer"}]}]}
    oracle_detail = {"items": [{"ExternalDescriptionStr": "<p>Role</p>", "ExternalQualificationsStr": "<p>3+ years</p>"}]}
    text, requested = describe("oracle_hcm", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs", {
        "/hcmRestApi/resources/latest/recruitingCEJobRequisitions": oracle_list,
        "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails": oracle_detail,
    })
    assert text == "<div><p>Role</p></div><div><p>3+ years</p></div>"
    assert requested == ["/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"]

    pcsx = {"data": {"count": 1, "positions": [{"id": 5, "displayJobId": "200", "name": "Software Engineer"}]}}
    assert describe("eightfold", "https://apply.careers.microsoft.com/careers", {
        "/api/pcsx/search": pcsx, "/api/pcsx/position_details": {"data": {"jobDescription": "<p>2+ years</p>"}},
    }) == ("<p>2+ years</p>", ["/api/pcsx/position_details"])
    apply = {"count": 1, "positions": [{"id": 6, "name": "Software Engineer"}]}
    assert describe("eightfold", "https://explore.jobs.netflix.net/careers", {
        "/api/apply/v2/jobs": apply, "/api/apply/v2/jobs/6": {"job_description": "<p>5+ years</p>"},
    }, api="apply") == ("<p>5+ years</p>", ["/api/apply/v2/jobs/6"])

    def hydration(route, data):
        literal = json.dumps(json.dumps({"loaderData": {route: data}}))[1:-1]
        return f'<script>window.__staticRouterHydrationData = JSON.parse("{literal}");</script>'

    item = {"positionId": "9", "postingTitle": "Software Engineer", "transformedPostingTitle": "software-engineer"}
    assert describe("apple", "https://jobs.apple.com/en-us/search", {
        "/en-us/search": hydration("search", {"searchResults": [item], "totalRecords": 1}),
        "/en-us/details/9/software-engineer": hydration("jobDetails", {"jobsData": {"minimumQualifications": "2+ years"}}),
    }) == ("2+ years", ["/en-us/details/9/software-engineer"])


# -- the monitor step ------------------------------------------------------------

POSTINGS = {  # Greenhouse job ID → its description (None: the request fails)
    1: "<h3>Requirements</h3><ul><li>5+ years of backend experience</li></ul>",
    2: "<ul><li>2+ years of experience</li></ul>",
    3: None,
    4: "<ul><li>3+ years of experience, or a Master's degree</li></ul>",
}


def greenhouse_check(state, ids=POSTINGS, settings=None, **filters):
    """Check a Greenhouse board listing `ids`; return (outcome, IDs whose description was requested)."""
    requested = []

    def handler(request):
        path = request.url.path
        if path == "/v1/boards/acme/jobs":
            return httpx.Response(200, json={"jobs": [
                {"id": i, "title": f"Software Engineer {i}", "absolute_url": f"https://example.com/{i}"} for i in ids]})
        job_id = int(path.rsplit("/", 1)[-1])
        requested.append(job_id)
        text = POSTINGS[job_id]
        return httpx.Response(500) if text is None else httpx.Response(200, json={"content": text})

    async def go():
        cfg = CompanyConfig(name="Acme", url="https://job-boards.greenhouse.io/acme",
                            filter=JobFilter(include_keywords=["software engineer"], **filters))
        async with make_client(handler, max_retries=0) as http:
            return await monitor.check_company(cfg, http, state, settings or Settings(), notify=True, first_run=False)

    outcome = asyncio.run(go())
    return outcome, sorted(requested)


def test_new_matches_asking_too_much_are_recorded_without_alerting():
    state = MonitorState()
    outcome, requested = greenhouse_check(state, max_experience_years=2)
    assert requested == [1, 2, 3, 4]
    assert [j.job_id for j in outcome.diff.to_notify] == ["2", "3", "4"]  # 3's description failed: kept
    (dropped, reason), = outcome.diff.suppressed
    assert dropped.job_id == "1" and reason.startswith("needs 5+ years")
    records = {uid.rsplit(":", 1)[-1]: rec for uid, rec in state.companies["Acme"].items()}
    assert records["1"].notified_at and records["1"].dropped_for == reason  # never alerts later
    assert all(rec.experience_checked for rec in records.values())
    assert not any(records[i].notified_at or records[i].dropped_for for i in "234")

    # Next check: nothing is read again, and the kept matches still wait for their alert.
    outcome, requested = greenhouse_check(state, max_experience_years=2)
    assert requested == [] and [j.job_id for j in outcome.diff.to_notify] == ["2", "3", "4"]


def test_descriptions_are_not_read_without_the_setting():
    outcome, requested = greenhouse_check(MonitorState())
    assert requested == [] and len(outcome.diff.to_notify) == 4


def test_matches_beyond_the_request_cap_wait_for_the_next_check():
    state = MonitorState()
    outcome, requested = greenhouse_check(state, ids=[2, 4], settings=Settings(max_detail_requests=1), max_experience_years=2)
    assert requested == [2] and [j.job_id for j in outcome.diff.to_notify] == ["2"]
    waiting = next(rec for uid, rec in state.companies["Acme"].items() if uid.endswith(":4"))
    assert not waiting.matched and not waiting.experience_checked  # the digest skips it meanwhile

    outcome, requested = greenhouse_check(state, ids=[2, 4], settings=Settings(max_detail_requests=1), max_experience_years=2)
    assert requested == [4] and [j.job_id for j in outcome.diff.to_notify] == ["2", "4"]


@pytest.mark.parametrize("text, education, keep", [
    ("Requirements:\n- Master's degree in Computer Science required.\n", "bachelors", False),
    ("Requirements:\n- Master's degree in Computer Science required.\n", "masters", True),
    ("Requirements:\n- Bachelor's or Master's degree in Computer Science.\n", "none", False),
    ("Requirements:\n- BS in Computer Science or equivalent experience.\n", "none", True),
    ("Requirements:\n- PhD in Machine Learning or a related field.\n", "masters", False),
    ("Requirements:\n- PhD in Machine Learning or a related field.\n", "phd", True),
    ("Requirements:\n- Currently pursuing a Bachelor's, Master's or PhD in Computer Science.\n", "bachelors", True),
    ("Minimum qualifications:\n- BS/MS in Computer Science.\n", "none", False),
    ("Preferred qualifications:\n- PhD in Computer Science.\n", "bachelors", True),
    ("Qualifications:\n- Proficiency with MS Office and SQL.\n", "none", True),
    ("We pay for our engineers' master's degrees.\n", "bachelors", True),
])
def test_education_drops_jobs_needing_a_higher_degree(text, education, keep):
    assert assess(text, None, education).keep is keep


def test_a_masters_path_counts_only_with_a_masters():
    text = "Requirements:\n- 5+ years of experience, or a Master's degree with 2+ years of experience.\n"
    assert assess(text, 2).keep  # no education given: a master's is assumed, as before
    assert assess(text, 2, "masters").keep
    verdict = assess(text, 2, "bachelors")
    assert not verdict.keep and "5+ years" in verdict.reason
    assert assess("Requirements:\n- 3+ years of experience, or a PhD.\n", 1, "phd").keep
    assert not assess("Requirements:\n- 3+ years of experience, or a PhD.\n", 1, "masters").keep
