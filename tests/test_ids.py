from role_radar.models import JobPosting, normalize_url
from tests.conftest import job


def test_uid_prefers_job_id():
    a = job("Software Engineer", job_id="123", location="Austin, TX")
    b = job("Software Engineer (renamed)", job_id="123", location="Remote")
    assert a.uid == b.uid == "acme:test:123"


def test_uid_scoped_by_company_and_source():
    a = job(job_id="1", company="Acme")
    b = job(job_id="1", company="Globex")
    c = JobPosting(company="Acme", title="x", url="u", source="lever", job_id="1")
    assert len({a.uid, b.uid, c.uid}) == 3


def test_hash_uid_is_normalized():
    a = job("Software  Engineer", job_id=None, location="Austin, TX", url="https://Acme.example/jobs/9/?utm_source=li#apply")
    b = job("software engineer", job_id=None, location=" austin,  tx ", url="https://acme.example/jobs/9")
    assert a.uid == b.uid
    assert a.uid.startswith("acme:h:")


def test_hash_uid_differs_by_location():
    a = job("Software Engineer", job_id=None, location="Austin, TX", url="https://acme.example/jobs")
    b = job("Software Engineer", job_id=None, location="Denver, CO", url="https://acme.example/jobs")
    assert a.uid != b.uid
    assert a.fingerprint != b.fingerprint


def test_fingerprint_ignores_id_and_url():
    a = job("Data Engineer", job_id="1", location="NYC", url="https://acme.example/jobs/1")
    b = job("Data Engineer", job_id="2", location="nyc", url="https://acme.example/jobs/2")
    assert a.fingerprint == b.fingerprint


def test_frozen_identity_survives_detail_enrichment():
    j = job("Data Engineer", job_id=None, location=None).freeze_identity()
    before = j.uid, j.fingerprint
    j.location = "Denver, CO"  # filled in from the detail page
    assert (j.uid, j.fingerprint) == before


def test_normalize_url_keeps_meaningful_query():
    assert normalize_url("https://x.com/jobs?id=5&utm_campaign=a&gh_src=b") == "https://x.com/jobs?id=5"
