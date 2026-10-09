import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from role_radar.config import load_config
from role_radar.scrapers import SCRAPERS, scraper_class_for
from role_radar.scrapers.base import ScraperError
from tests.conftest import company, job, make_client


def run_scraper(ats, url, handler, **options):
    async def go():
        async with make_client(handler) as http:
            cfg = company(url=url)
            cfg.options = options
            return await SCRAPERS[ats](cfg, http).fetch_jobs()
    return asyncio.run(go())


def test_mathworks_uses_feed_and_keeps_edg_identity_and_country():
    feed = '''<rss><channel><item><id>16217</id>
      <title>Engineering Development Group (16217-AB)</title>
      <title_raw>Engineering Development Group</title_raw>
      <link>https://www.mathworks.com/company/jobs/opportunities/16217-edg.html</link>
      <location><city>Natick</city><country>US</country></location>
      <job_function>New Career Program (EDG)</job_function><job_type>New Career</job_type>
      <qualifications>Not stored</qualifications></item>
      <item><id>2</id><title_raw>Software Engineer</title_raw><link>https://example.com/2</link>
      <location><city>Munich</city><country>DE</country></location></item></channel></rss>'''
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=feed)
    result = run_scraper("mathworks", "https://www.mathworks.com/company/jobs/opportunities/search", handler)
    assert urls == ["https://www.mathworks.com/company/jobs/opportunities/rss.xml"]
    assert not result.complete  # RSS is not a removal snapshot.
    edg, german = result.jobs
    assert edg.job_id == "16217" and edg.title == "Engineering Development Group"
    assert edg.location == "Natick, United States"
    assert edg.employment_type is None
    assert german.location == "Munich, Germany"  # DE must not be mistaken for Delaware.


@pytest.mark.parametrize("feed", ["<html>Access denied</html>", "<rss>", "<rss><channel><item><id>1</id></item></channel></rss>"])
def test_mathworks_rejects_bad_or_incomplete_feed(feed):
    with pytest.raises(ScraperError):
        run_scraper("mathworks", "https://www.mathworks.com", lambda r: httpx.Response(200, text=feed))


def test_hrmdirect_unclosed_links_and_multiple_locations():
    html = '''<table><tr data-req-id="42"><td class="posTitle reqitem">
      <a href="job-opening.php?req=42&amp;req_loc=1&amp;cust_sort1=123#job">Machine Learning Engineer </td>
      <td class="cities reqitem">Cleveland</td><td class="state">OH</td><td class="countries">United States</td></tr>
      <tr data-req-id="42"><td class="posTitle"><a href="job-opening.php?req=42&amp;req_loc=2">Machine Learning Engineer</a></td>
      <td class="cities">Toronto</td><td class="countries">Canada</td></tr></table>'''
    def handler(request):
        assert request.url.params["search"] == "true"
        assert request.url.params["cust_sort1"] == "123"
        return httpx.Response(200, text=html)
    result = run_scraper("hrmdirect", "https://example.hrmdirect.com/employment/job-openings.php?cust_sort1=123", handler)
    assert [j.title for j in result.jobs] == ["Machine Learning Engineer"] * 2
    assert [j.job_id for j in result.jobs] == ["42:1", "42:2"]
    assert result.jobs[0].location == "Cleveland, OH, United States"
    assert result.jobs[0].url == "https://example.hrmdirect.com/employment/job-opening.php?req=42&req_loc=1"
    assert not result.complete


def test_hrmdirect_search_form_is_not_empty_board():
    with pytest.raises(ScraperError):
        run_scraper("hrmdirect", "https://example.hrmdirect.com/employment/job-openings.php", lambda r: httpx.Response(200, text='Select options and click Search'))


def test_hrmdirect_recognizes_explicit_empty_board():
    result = run_scraper("hrmdirect", "https://example.hrmdirect.com/employment/job-openings.php", lambda r: httpx.Response(200, text='There are currently no job openings.'))
    assert result.complete and not result.jobs


def icims_card(job_id, host="example.icims.com"):
    return f'''<li class="iCIMS_JobCardItem"><a href="https://{host}/jobs/{job_id}/software-engineer/job?hub=9">
      <span>Requisition Title</span><h3>Software Engineer</h3></a>
      <dl><dt>Country (Full Name)</dt><dd>India</dd>
      <dt>Primary Location : City</dt><dd>Bangalore</dd><dt>Job Category</dt><dd>Engineering</dd></dl></li>'''


def test_icims_pagination_location_and_cross_portal_identity():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        assert request.url.params["in_iframe"] == "1"
        if request.url.params.get("pr") != "1":
            body = icims_card("42") + '<a href="/jobs/search?pr=1&amp;in_iframe=1"><span>Next page of results</span></a>'
        else:
            body = icims_card("42", "global-example.icims.com") + '<a class="invisible" href="/jobs/search?pr=2">Next page of results</a>'
        return httpx.Response(200, text=body)
    result = run_scraper("icims", "https://example.icims.com/jobs/search", handler)
    assert len(seen) == 2 and result.complete and len(result.jobs) == 2
    assert len({j.job_id for j in result.jobs}) == 2
    assert result.jobs[0].title == "Software Engineer"
    assert result.jobs[0].location == "India, Bangalore"
    assert "?" not in result.jobs[0].url


def test_icims_location_from_the_card_header():
    card = '''<li class="iCIMS_JobCardItem"><div class="row"><div class="col-xs-6 header left">
      <span class="sr-only field-label">Job Locations</span> <span > US-TX-Richmond</span></div>
      <div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/5553/food-service-associate/job?in_iframe=1"
      class="iCIMS_Anchor"><span class="sr-only field-label">Title</span><h3 > Food Service Associate</h3></a></div>
      <dl class="iCIMS_JobHeaderGroup"><div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Category</dt>
      <dd class="iCIMS_JobHeaderData"><span > Food and Nutrition</span></dd></div></dl></div></li>'''
    result = run_scraper("icims", "https://careers-x.icims.com/jobs/search", lambda r: httpx.Response(200, text=card))
    assert [(j.title, j.location) for j in result.jobs] == [("Food Service Associate", "US-TX-Richmond")]


def test_icims_repeated_page_is_incomplete():
    result = run_scraper("icims", "https://example.icims.com/jobs/search", lambda r: httpx.Response(200, text=icims_card("1") + '<a href="/jobs/search?pr=1">Next page of results</a>'))
    assert not result.complete and len(result.jobs) == 1


def test_avature_follows_next_link_and_ignores_duplicate_apply_links():
    def handler(request):
        second = request.url.params.get("jobOffset") == "12"
        ident = "2" if second else "1"
        body = f'''<article class="article article--result"><h3><a href="https://example.avature.net/careers/JobDetail/Engineer/{ident}">Compiler Engineer</a></h3>
          <span class="list-item-location">Sydney, Australia</span><a href="https://example.avature.net/careers/JobDetail/Engineer/{ident}">Apply</a></article>'''
        if not second:
            body += '<a href="/careers/SearchJobs/?jobOffset=12">Next &gt;&gt;</a>'
        return httpx.Response(200, text=body)
    result = run_scraper("avature", "https://example.avature.net/careers/SearchJobs/", handler)
    assert result.complete and len(result.jobs) == 2
    assert result.jobs[0].location == "Sydney, Australia"
    assert result.jobs[0].title == "Compiler Engineer"


def test_avature_card_without_location_span_takes_its_first_plain_span():
    # Two Sigma: the place, then the team and the level, each in a plain span.
    body = '''<article class="article article--result" id="article--1"><h3><a class="link"
      href="https://careers.twosigma.com/careers/JobDetail/New-York-New-York-United-States-AI-Solutions-Developer/14102"> AI Solutions Developer </a></h3>
      <div class="article__header__content__text"><span class="paragraph_inner-span">United States - NY New York</span>
      <div class="article__header__content__sub-text"><span class="paragraph_inner-span">Engineering</span>
      <span class="paragraph_inner-span">Experienced</span></div></div></article>'''
    result = run_scraper("avature", "https://careers.twosigma.com/careers/OpenRoles/", lambda r: httpx.Response(200, text=body))
    assert result.complete and result.jobs[0].job_id == "14102"
    assert result.jobs[0].location == "United States - NY New York"


def test_avature_newest_first_stops_at_a_page_with_nothing_new():
    from role_radar.storage import SeenJob
    # Siemens: 6 jobs a page, newest first, the city/state/country each in a span of their own.
    def card(ident, place='<span class="list-item-jobCity">Wendell</span><span class="separator">, </span>'
                          '<span class="list-item-jobCountry">United States of America</span>'):
        return (f'<article class="article article--result 1"><h3><a class="link" href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{ident}">'
                f'Software Engineer {ident}</a></h3><span class="list-item-location">\n    {place}\n</span> '
                f'<span class="list-item-jobId">Job ID: {ident}</span></article>')
    seen = []
    def handler(request):
        offset = int(request.url.params.get("folderOffset", "0"))
        seen.append(offset)
        body = card(100 - offset) + card(99 - offset, "Multiple Locations")
        return httpx.Response(200, text=body + f'<a href="/en_US/externaljobs/SearchJobs/?folderSort=postedDate&amp;folderSortDirection=DESC&amp;folderOffset={offset + 2}">Next &gt;&gt;</a>')

    url = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?folderSort=postedDate&folderSortDirection=DESC"
    async def go(known):
        async with make_client(handler) as http:
            cfg = company("Siemens", url=url)
            cfg.options = {"max_pages": 3}
            scraper = SCRAPERS["avature"](cfg, http)
            scraper.known = known
            return scraper, await scraper.fetch_jobs()

    scraper, first = asyncio.run(go({}))
    assert seen == [0, 2, 4] and not first.complete  # a first check reads every page it may
    assert first.jobs[0].location == "Wendell, United States of America"
    assert scraper.missing_fields(first.jobs[1]) == {"location"} and not scraper.missing_fields(first.jobs[0])

    seen.clear()
    known = {j.uid: SeenJob(title=j.title, url=j.url, fingerprint=j.fingerprint, first_seen="x") for j in first.jobs[2:]}
    _, later = asyncio.run(go(known))
    assert seen == [0, 2] and not later.complete  # the second page held nothing new


def test_avature_job_page_gives_places_and_description():
    page = '''<div class="article__content__view__field"><div class="article__content__view__field__label">Location(s)</div>
      <div class="article__content__view__field__value"><ul class="list list--bullet list--locations">
      <li class="list__item"> Beijing - Beijing Shi - China </li><li class="list__item"> Austin - Texas - United States of America </li></ul></div></div>
      <div class="article__content__view__field tf_replaceFieldVideoTokens"><div class="article__content__view__field__value">
      <div><p>You have 3+ years of experience.</p></div></div></div>'''
    async def go():
        async with make_client(lambda r: httpx.Response(200, text=page)) as http:
            scraper = SCRAPERS["avature"](company("Siemens", url="https://jobs.siemens.com/en_US/externaljobs/SearchJobs/"), http)
            multi = job(url="https://jobs.siemens.com/en_US/externaljobs/JobDetail/1", location="Multiple Locations")
            await scraper.fetch_details(multi)
            single = job(url="https://jobs.siemens.com/en_US/externaljobs/JobDetail/2", location="Austin, Texas, United States of America")
            return multi, await scraper.fetch_description(single)
    multi, description = asyncio.run(go())
    assert multi.location == "Beijing, Beijing Shi, China; Austin, Texas, United States of America"
    assert "3+ years of experience" in multi.extra["description"] and "3+ years of experience" in description


@pytest.mark.parametrize("ats,url", [("icims", "https://example.icims.com/jobs/search"), ("avature", "https://example.avature.net/careers/SearchJobs/")])
def test_missing_listing_structure_is_error(ats, url):
    with pytest.raises(ScraperError):
        run_scraper(ats, url, lambda r: httpx.Response(200, text='<html>Loading jobs...</html>'))


def test_jibe_uses_total_and_actual_page_size():
    seen = []
    def handler(request):
        page = int(request.url.params["page"])
        seen.append(page)
        # A server can ignore the requested limit=100 and serve one row at a time.
        return httpx.Response(200, json={"totalCount": 2, "jobs": [{"data": {
            "slug": str(page), "title": "Software Engineer", "city": "Olathe", "state": "Kansas",
            "country": "United States", "description": "Ignored", "posted_date": "2026-09-20T00:00:00Z",
        }}]})
    result = run_scraper("jibe", "https://careers.example.com/jobs", handler)
    assert seen == [1, 2] and result.complete
    assert result.jobs[0].url == "https://careers.example.com/jobs/1"
    assert result.jobs[0].location == "Olathe, Kansas, United States"


def test_jibe_repeated_page_or_cap_never_infers_removals():
    handler = lambda r: httpx.Response(200, json={"totalCount": 10, "jobs": [{"data": {"slug": "1", "title": "Software Engineer"}}]})
    for options in ({}, {"max_pages": 1}):
        result = run_scraper("jibe", "https://careers.example.com/jobs", handler, **options)
        assert len(result.jobs) == 1 and not result.complete


def test_comsol_pairs_jobs_with_their_location_heading():
    html = '''<h3>Beijing, China</h3><ul><li><a href="/company/careers/job/1/">Applications Engineer</a></li></ul>
      <h3>Burlington, MA, USA</h3><ul><li><a href="/company/careers/job/2/">Software Developer</a></li></ul>'''
    result = run_scraper("comsol", "https://www.comsol.com/company/careers", lambda r: httpx.Response(200, text=html))
    assert [j.location for j in result.jobs] == ["Beijing, China", "Burlington, MA, USA"]
    assert result.jobs[1].job_id == "2" and not result.complete


def test_recruiterbox_keeps_direct_widget_link_and_location():
    def handler(request):
        assert request.url.path == "/widget/42716/openings/"
        return httpx.Response(200, json=[{"id": 123, "title": "Software Engineer", "desc": "Ignored",
            "location": {"city": "Champaign", "state": "Illinois", "country": "United States"},
            "allows_remote": True, "position_type": "Full-time"}])
    result = run_scraper("recruiterbox", "https://example.com/careers/", handler, widget_id=42716)
    assert result.complete and len(result.jobs) == 1
    assert result.jobs[0].url == "https://example.com/careers/#op-123"
    assert result.jobs[0].job_id == "123"
    assert result.jobs[0].location == "Champaign, Illinois, United States (Remote)"


def test_workdaysite_recruiting_url_preserves_tenant_site_and_public_path():
    def handler(request):
        assert request.url.path == "/wday/cxs/guidewire/external/jobs"
        return httpx.Response(200, json={"total": 1, "jobPostings": [{
            "title": "Software Engineer", "externalPath": "/job/India/Software-Engineer_JR_123", "locationsText": "India",
        }]})
    result = run_scraper("workday", "https://wd5.myworkdaysite.com/recruiting/guidewire/external", handler)
    assert result.jobs[0].url == "https://wd5.myworkdaysite.com/recruiting/guidewire/external/job/India/Software-Engineer_JR_123"


@pytest.mark.parametrize("url,expected", [
    ("https://www.mathworks.com/company/jobs", "mathworks"),
    ("https://flexjet.hrmdirect.com/employment/job-openings.php", "hrmdirect"),
    ("https://globalcareers-sas.icims.com/jobs/search", "icims"),
    ("https://bloomberg.avature.net/careers/SearchJobs/", "avature"),
    ("https://www.comsol.com/company/careers", "comsol"),
])
def test_custom_source_detection(url, expected):
    assert scraper_class_for(url).name == expected


@pytest.mark.parametrize("title,location,expected", [
    ("Multiple Openings - Engineering Development Group - U.S.", "Natick, United States", True),
    ("Machine Learning Engineer", "Cleveland, OH, United States", True),
    ("Software Engineer", "Toronto, Canada", True),
    ("Software Engineer", "Sydney, Australia", True),
    ("Software Engineer", "Bangalore, India", True),
    ("Software Engineer", "Remote", True),
    ("Software Engineer", "Remote - United Kingdom", False),
    ("Software Engineer", "Munich, Germany", False),
    ("Software Engineer", "Cambridge, UK", False),
    ("Software Engineer", "Singapore", False),
    ("EDG Manager", "Natick, United States", False),
])
def test_config_program_titles_and_target_countries(title, location, expected):
    config = load_config(Path(__file__).parent.parent / "config/companies.yaml",
                         Path(__file__).parent.parent / "config/profile.example.yaml")
    filt = next(c.filter for c in config.companies if c.name == "Discord")
    assert filt.evaluate(job(title, location=location)).matched is expected


# -- big-company career sites and the Oracle HCM / Eightfold platforms --

def test_amazon_reads_newest_pages_and_is_incomplete_at_the_hits_cap():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params.multi_items()))
        offset = int(request.url.params["offset"])
        jobs = [{"id_icims": str(offset + i), "title": f"Software Development Engineer {offset + i}",
                 "job_path": f"/en/jobs/{offset + i}/sde", "normalized_location": "Seattle, Washington, USA",
                 "posted_date": "September 25, 2026", "job_schedule_type": "full-time", "job_category": "Software Development"}
                for i in range(100)]
        return httpx.Response(200, json={"hits": 10000, "jobs": jobs})

    result = run_scraper("amazon", "https://www.amazon.jobs/en/search", handler, max_jobs=200, categories=["software-development"])
    assert [s["offset"] for s in seen] == ["0", "100"] and seen[0]["sort"] == "recent" and seen[0]["country"] == "USA"
    assert seen[0]["category[]"] == "software-development"
    assert len(result.jobs) == 200 and not result.complete
    job = result.jobs[0]
    assert job.url == "https://www.amazon.jobs/en/jobs/0/sde" and str(job.date_posted) == "2026-09-25"
    assert job.location == "Seattle, Washington, USA" and job.employment_type == "full-time"


def test_apple_parses_hydration_data_and_stops_at_the_total():
    import json as _json

    def page(results, total):
        data = {"loaderData": {"search": {"searchResults": results, "totalRecords": total}}}
        literal = _json.dumps(_json.dumps(data))[1:-1]  # a JSON document inside a JS string literal
        return f'<script>window.__staticRouterHydrationData = JSON.parse("{literal}");</script>'

    item = {"positionId": "200685702", "postingTitle": "Software Engineer, Maps", "transformedPostingTitle": "software-engineer-maps",
            "locations": [{"city": "Cupertino", "stateProvince": "California", "countryName": "United States of America"}],
            "team": {"teamName": "Software and Services"}, "postDateInGMT": "2026-09-27T10:19:33.768943492Z"}
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=page([item], 1))

    result = run_scraper("apple", "https://jobs.apple.com/en-us/search?location=united-states-USA&sort=relevance", handler)
    assert len(urls) == 1 and "location=united-states-USA" in urls[0] and "sort=newest" in urls[0] and "relevance" not in urls[0]
    (job,) = result.jobs
    assert result.complete and job.job_id == "200685702"
    assert job.url == "https://jobs.apple.com/en-us/details/200685702/software-engineer-maps"
    assert job.location == "Cupertino, California, United States of America" and str(job.date_posted) == "2026-09-27"


def test_google_reads_only_first_pages_of_each_query():
    import json as _json

    def page(rows):
        return (f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{_json.dumps([rows, None, len(rows), 20])}, "
                "sideChannel: {}});</script>")

    def row(job_id, title, created):
        r = [None] * 21
        r[0], r[1], r[9], r[12] = job_id, title, [["Mountain View, CA, USA"], ["New York, NY, USA"]], [created, 0]
        return r

    urls = []

    def handler(request):
        urls.append(request.url)
        level = request.url.params["target_level"]
        return httpx.Response(200, text=page([row("1" if level == "EARLY" else "2", f"Software Engineer {level}", 1790377680)]))

    result = run_scraper("google", "https://www.google.com/about/careers/applications/jobs/results/?location=United%20States", handler)
    assert [u.params["target_level"] for u in urls] == ["EARLY", "MID"]
    assert all(u.params["sort_by"] == "date" and "page" not in u.params and u.params["location"] == "United States" for u in urls)
    assert not result.complete  # first pages only
    job = {j.job_id: j for j in result.jobs}["1"]
    assert job.url == "https://www.google.com/about/careers/applications/jobs/results/1"
    assert job.location == "Mountain View, CA, USA; New York, NY, USA" and str(job.date_posted) == "2026-09-25"


def test_eightfold_apply_and_pcsx_apis():
    def apply_handler(request):
        assert request.url.path == "/api/apply/v2/jobs" and request.url.params["domain"] == "netflix.com"
        start = int(request.url.params["start"])
        positions = [{"id": start + i, "name": f"Software Engineer {start + i}", "location": "Remote, United States",
                      "t_create": 1790294400, "canonicalPositionUrl": f"https://explore.jobs.netflix.net/careers/job/{start + i}"}
                     for i in range(min(10, 15 - start))]
        return httpx.Response(200, json={"positions": positions, "count": 15})

    result = run_scraper("eightfold", "https://explore.jobs.netflix.net/careers", apply_handler, api="apply", domain="netflix.com")
    assert len(result.jobs) == 15 and result.complete
    assert result.jobs[0].url == "https://explore.jobs.netflix.net/careers/job/0" and str(result.jobs[0].date_posted) == "2026-09-25"

    def pcsx_handler(request):
        assert request.url.path == "/api/pcsx/search" and request.url.params["domain"] == "microsoft.com"
        return httpx.Response(200, json={"data": {"count": 1185, "positions": [
            {"id": 1970393557002655, "displayJobId": "200057070", "name": "Software Engineer",
             "locations": ["United States, Washington, Redmond"], "postedTs": 1790191288,
             "positionUrl": "/careers/job/1970393557002655", "department": "Engineering"}]}})

    result = run_scraper("eightfold", "https://apply.careers.microsoft.com/careers", pcsx_handler, max_jobs=10)
    (job,) = result.jobs
    assert not result.complete and job.job_id == "200057070"
    assert job.url == "https://apply.careers.microsoft.com/careers/job/1970393557002655"
    assert job.location == "United States, Washington, Redmond"


def test_oracle_hcm_builds_the_finder_unencoded_and_pages():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        offset = int(re.search(r"offset=(\d+)", str(request.url)).group(1))
        reqs = [{"Id": str(offset + i), "Title": "Software Engineer", "PostedDate": "2026-09-27",
                 "PrimaryLocation": "Plano, TX, United States", "secondaryLocations": [{"Name": "Dallas, TX, United States"}]}
                for i in range(min(100, 130 - offset))]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 130, "requisitionList": reqs}]})

    result = run_scraper("oracle_hcm", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs", handler)
    assert "finder=findReqs;siteNumber=CX_1001,limit=100,offset=0,sortBy=POSTING_DATES_DESC" in urls[0]
    assert len(urls) == 2 and len(result.jobs) == 130 and result.complete
    job = result.jobs[0]
    assert job.url == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/0"
    assert job.location == "Plano, TX, United States; Dallas, TX, United States"


def test_meta_reads_new_jobs_from_their_pages_and_reuses_known_ones():
    from role_radar.storage import SeenJob

    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://www.metacareers.com/profile/job_details/{i}/</loc></url>" for i in (11, 22, 33, 44)
    ) + "</urlset>"
    page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Software Engineer %s", '
            '"datePosted": "2026-09-20", "employmentType": "FULL_TIME", "jobLocation": [{"@type": "Place", '
            '"address": {"addressLocality": "Menlo Park", "addressRegion": "CA", "addressCountry": "US"}}]}</script>')
    fetched = []

    def handler(request):
        if request.url.path.endswith("sitemap.xml"):
            return httpx.Response(200, text=sitemap)
        job_id = request.url.path.rstrip("/").rsplit("/", 1)[1]
        fetched.append(job_id)
        return httpx.Response(200, text=page % job_id)

    async def go(known, **options):
        async with make_client(handler) as http:
            cfg = company("Meta", url="https://www.metacareers.com/jobsearch/")
            cfg.options = options
            scraper = SCRAPERS["meta"](cfg, http)
            scraper.known = known
            return await scraper.fetch_jobs()

    first = asyncio.run(go({}, new_per_check=2))
    assert sorted(fetched) == ["11", "22"] and not first.complete  # 2 of 4 read this check
    assert [j.title for j in first.jobs] == ["Software Engineer 11", "Software Engineer 22"]
    assert first.jobs[0].location == "Menlo Park, CA, US" and str(first.jobs[0].date_posted) == "2026-09-20"

    known = {j.uid: SeenJob(title=j.title, url=j.url, fingerprint=j.fingerprint, first_seen="x", location=j.location)
             for j in first.jobs}
    fetched.clear()
    second = asyncio.run(go(known, new_per_check=2))
    assert sorted(fetched) == ["33", "44"] and second.complete  # known jobs cost nothing
    fetched.clear()
    assert not asyncio.run(go({}, read_seconds=0)).jobs and fetched == []  # out of time: nothing read
    assert {j.title for j in second.jobs} == {f"Software Engineer {i}" for i in (11, 22, 33, 44)}


def test_tiktok_reads_every_page_and_keeps_us_jobs():
    bodies = []
    us = {"en_name": "San Jose", "parent": {"en_name": "California", "parent": {"en_name": "United States of America"}}}

    def post(job_id, city):
        return {"id": str(job_id), "title": "Software Engineer", "city_info": city,
                "recruit_type": {"en_name": "Regular"}, "job_category": {"en_name": "R&D"}}

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.headers["website-path"] == "tiktok"
        posts = ([post(i, us) for i in range(100)] if body["offset"] == 0
                 else [post(i, us) for i in range(100, 120)] + [post("sg", {"en_name": "Singapore"})])
        return httpx.Response(200, json={"code": 0, "data": {"job_post_list": posts, "count": 121}})

    result = run_scraper("tiktok", "https://lifeattiktok.com/search", handler)
    assert [b["offset"] for b in bodies] == [0, 100] and bodies[0]["location_code_list"] == []
    assert len(result.jobs) == 120 and result.complete  # the Singapore job is dropped
    job = result.jobs[0]
    assert job.url == "https://lifeattiktok.com/search/0" and job.location == "San Jose, California, United States of America"
    assert job.employment_type == "Regular" and job.department == "R&D"


def test_deshaw_reads_the_careers_page_data():
    data = {"props": {"pageProps": {
        "regularJobs": [
            {"id": 6113, "displayName": "Software Developer (Denver)", "office": [{"name": "Denver"}], "category": ["Technology"],
             "data": {"jobUrl": "Software-Developer-Denver-6113", "jobMetadata": {"workStatus": "Regular Full-Time"},
                      "jobDescription": {"websiteDescription": "Build systems.", "peopleWeAreLookingForStr": "Two years of experience."}}},
            {"id": 6012, "displayName": "Receptionist", "office": [{"name": "Singapore"}, {"name": "New York"}],
             "data": {"jobUrl": "Receptionist-6012"}},
        ],
        "internships": [{"id": 7001, "displayName": "Software Developer Intern", "office": [{"name": "Gurugram"}],
                         "data": {"jobUrl": "Software-Developer-Intern-7001"}}],
        "internalJobs": [{"id": 9, "displayName": "Internal move", "data": {"jobUrl": "Internal-9"}}],
    }}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'
    result = run_scraper("deshaw", "https://www.deshaw.com/careers/choose-your-path", lambda r: httpx.Response(200, text=page))
    assert result.complete and [j.job_id for j in result.jobs] == ["6113", "6012", "7001"]  # internal jobs skipped
    dev, reception, intern = result.jobs
    assert dev.url == "https://www.deshaw.com/careers/Software-Developer-Denver-6113"
    assert (dev.location, dev.department, dev.employment_type) == ("Denver, CO, United States", "Technology", "Regular Full-Time")
    assert dev.extra["description"] == "Build systems. Two years of experience."
    assert reception.location == "Singapore; New York, NY, United States"
    assert intern.location == "Gurugram, India"


def test_deshaw_page_without_jobs_is_error():
    with pytest.raises(ScraperError):
        run_scraper("deshaw", "https://www.deshaw.com/careers/choose-your-path", lambda r: httpx.Response(200, text="<html></html>"))


def test_avature_newest_first_keeps_the_sort_its_next_links_drop():
    # Deloitte: the Next link is ?jobRecordsPerPage=10&jobOffset=10, without the sort.
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        offset = int(request.url.params.get("jobOffset", "0"))
        card = (f'<article class="article article--result"><h3><a href="https://apply.deloitte.com/en_US/careers/JobDetail/Analyst/{900 - offset}">'
                f'Analyst {offset}</a></h3><span class="list-item-location">Houston, Texas, United States</span></article>')
        return httpx.Response(200, text=card + f'<a href="/en_US/careers/SearchJobs/?jobRecordsPerPage=10&amp;jobOffset={offset + 10}">Next &gt;&gt;</a>')
    result = run_scraper("avature", "https://apply.deloitte.com/en_US/careers/SearchJobs/?folderSort=postedDate&folderSortDirection=DESC",
                         handler, max_pages=3)
    assert len(result.jobs) == 3 and not result.complete
    assert seen[1] == {"jobRecordsPerPage": "10", "jobOffset": "10", "folderSort": "postedDate", "folderSortDirection": "DESC"}


def test_avature_deloitte_cards_and_job_page():
    card = ('<article class="article article--result"><div class="article__header__text"><h3><a class="link" '
            'href="https://apply.deloitte.com/en_US/careers/JobDetail/Consultant/370759"> Consultant </a></h3>'
            '<div class="article__header__text__subtitle"><span> Deloitte US </span> | <span> Deloitte Consulting LLP </span> | '
            '<span>Multiple Locations</span></div></div></article>')
    page = ('<div class="article__header__text__subtitle"><a class="link toggleLocations">Same job available in 2 locations</a>'
            '<div class="article__header--locations"><div class="fluid-cols"><p class="paragraph">Atlanta, Georgia, United States</p>'
            '<p class="paragraph">Chicago, Illinois, United States</p></div></div></div>'
            '<div class="article__content"><p>2+ years of consulting experience</p></div>')
    async def go():
        async with make_client(lambda r: httpx.Response(200, text=page if "JobDetail" in str(r.url) else card)) as http:
            scraper = SCRAPERS["avature"](company("Deloitte", url="https://apply.deloitte.com/en_US/careers/SearchJobs/"), http)
            result = await scraper.fetch_jobs()
            (consultant,) = result.jobs
            assert consultant.location == "Multiple Locations" and scraper.missing_fields(consultant) == {"location"}
            return await scraper.fetch_details(consultant)
    consultant = asyncio.run(go())
    assert consultant.location == "Atlanta, Georgia, United States; Chicago, Illinois, United States"
    assert "2+ years of consulting experience" in consultant.extra["description"]


def test_radancy_lists_jobs_from_the_sitemap_and_reads_new_ones_page():
    from role_radar.storage import SeenJob
    sitemap = """<?xml version="1.0"?><urlset>
      <url><loc>https://jobs.intuit.com/search-jobs</loc></url>
      <url><loc>https://jobs.intuit.com/job/mountain-view/staff-software-engineer-ai/27595/101</loc></url>
      <url><loc>https://jobs.intuit.com/en/job/bengaluru/data-engineer-2/27595/102</loc></url></urlset>"""
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Staff Software Engineer, AI", "datePosted": "2026-10-08",
               "description": "<p>3+ years</p>", "jobLocation": [
                   {"@type": "Place", "address": {"addressLocality": "Mountain View", "addressRegion": "California", "addressCountry": "United States"}},
                   {"@type": "Place", "address": {"addressLocality": "San Diego", "addressRegion": "California", "addressCountry": "United States"}}]}
    page = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    def handler(request):
        return httpx.Response(200, text=sitemap if request.url.path == "/sitemap.xml" else page)

    async def go(known):
        async with make_client(handler) as http:
            scraper = SCRAPERS["radancy"](company("Intuit", url="https://jobs.intuit.com/"), http)
            scraper.known = known
            result = await scraper.fetch_jobs()
            return scraper, result

    scraper, first = asyncio.run(go({}))
    assert first.complete and [(j.job_id, j.title, j.location) for j in first.jobs] == [
        ("101", "staff software engineer ai", "mountain view"), ("102", "data engineer 2", "bengaluru")]
    assert scraper.missing_fields(first.jobs[0]) == {"location"} and not scraper.wants_details(first.jobs[0])  # first check: a baseline

    known = {first.jobs[1].uid: SeenJob(title="Data Engineer II", url="u", fingerprint="f", first_seen="x", location="Bengaluru, Karnataka, India")}
    scraper, later = asyncio.run(go(known))
    new, old = later.jobs
    assert (old.title, old.location, scraper.wants_details(old)) == ("Data Engineer II", "Bengaluru, Karnataka, India", False)
    assert scraper.wants_details(new)

    async def details():
        async with make_client(handler) as http:
            s = SCRAPERS["radancy"](company("Intuit", url="https://jobs.intuit.com/"), http)
            return await s.fetch_details(new)
    asyncio.run(details())
    assert new.title == "Staff Software Engineer, AI" and new.date_posted.isoformat() == "2026-10-08"
    assert new.location == "Mountain View, California, United States; San Diego, California, United States"
    assert new.extra["description"] == "<p>3+ years</p>" and not scraper.missing_fields(new)


def test_jobposting_sitemap_links_and_job_page():
    from role_radar.scrapers.jobposting import link_job
    assert link_job("/careers/jobs/spatial-services-software-architect-550471") == ("550471", "spatial services software architect")
    assert link_job("/retail-customer-service-associate/job/P25-354770-1") == ("P25-354770-1", "retail customer service associate")
    index = '<sitemapindex><sitemap><loc>https://www.3ds.com/sitemap/sitemap-careers.xml</loc></sitemap><sitemap><loc>https://www.3ds.com/sitemap/sitemap-partner.xml</loc></sitemap></sitemapindex>'
    careers = ('<urlset><url><loc>https://www.3ds.com/careers/our-teams</loc></url>'
               '<url><loc>https://www.3ds.com/careers/jobs/cloud-platform-engineer-550471</loc></url></urlset>')
    page = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"JobPosting","title":"Cloud Platform Engineer",'
            '"datePosted":"2026-10-01","jobLocation":{"@type":"Place","address":{"addressLocality":"Iselin","addressRegion":"NJ","addressCountry":"United States"}}}]}</script>')
    routes = {"/sitemap.xml": index, "/sitemap/sitemap-careers.xml": careers, "/careers/jobs/cloud-platform-engineer-550471": page}
    def handler(request):
        body = routes.get(request.url.path)
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    async def go():
        async with make_client(handler) as http:
            scraper = SCRAPERS["jobposting"](company("Dassault", url="https://www.3ds.com/careers"), http)
            (job,) = (await scraper.fetch_jobs()).jobs  # only the job page; the partner sitemap isn't read
            assert (job.job_id, job.title, scraper.missing_fields(job)) == ("550471", "cloud platform engineer", {"location"})
            return await scraper.fetch_details(job)
    job = asyncio.run(go())
    assert (job.title, job.location, job.date_posted.isoformat()) == ("Cloud Platform Engineer", "Iselin, NJ, United States", "2026-10-01")
