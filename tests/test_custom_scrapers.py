import asyncio
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
    config = load_config(Path(__file__).parent.parent / "config/companies.yaml")
    filt = next(c.filter for c in config.companies if c.name == "Discord")
    assert filt.evaluate(job(title, location=location)).matched is expected
