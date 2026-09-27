import pytest

from filters import JobFilter, compile_keyword
from tests.conftest import job

INCLUDE = ["software engineer", "backend engineer", "platform engineer", "machine learning engineer", "AI engineer", "data engineer"]
EXCLUDE = ["senior", "staff", "principal", "manager", "director"]


@pytest.fixture
def default_filter():
    return JobFilter(include_keywords=INCLUDE, exclude_keywords=EXCLUDE)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Software Engineer", True),
        ("SOFTWARE ENGINEER II", True),
        ("Backend Engineer, Payments", True),
        ("Machine Learning Engineer", True),
        ("Data Engineer - Remote", True),
        ("Senior Software Engineer", False),
        ("Staff Platform Engineer", False),
        ("Engineering Manager, Data Engineering", False),
        ("Director, AI Engineer Programs", False),
        ("Account Executive", False),
    ],
)
def test_title_matching(default_filter, title, expected):
    assert default_filter.evaluate(job(title)).matched is expected


def test_word_boundaries():
    f = JobFilter(include_keywords=["AI", "ML"])
    assert f.evaluate(job("AI Engineer"))
    assert f.evaluate(job("Engineer (ML/AI)"))
    assert not f.evaluate(job("Maintenance Technician"))
    assert not f.evaluate(job("HTML Developer"))


def test_separator_tolerance():
    pattern = compile_keyword("back end")
    assert pattern.search("Back-End Developer")
    assert pattern.search("back end developer")
    assert pattern.search("Backend Developer")


def test_regex_keyword():
    f = JobFilter(include_keywords=[r"re:engineer\s+(i|1)\b"])
    assert f.evaluate(job("Software Engineer I"))
    assert not f.evaluate(job("Software Engineer III"))


def test_description_matching():
    title_only = JobFilter(include_keywords=["kubernetes"])
    both = JobFilter(include_keywords=["kubernetes"], match_on=["title", "description"])
    j = job("Infrastructure Engineer", description="You will run Kubernetes clusters.")
    assert not title_only.evaluate(j)
    assert both.evaluate(j)
    assert both.needs_description


def test_location_and_employment_type_filters():
    f = JobFilter(include_keywords=["engineer"], locations=["Austin", "Remote"], employment_types=["full-time"])
    assert f.evaluate(job("Engineer", location="Austin, TX", employment_type="Full Time"))
    assert f.evaluate(job("Engineer", location="Remote - US", employment_type="Full-Time"))
    assert not f.evaluate(job("Engineer", location="Boston, MA", employment_type="Full-Time"))
    assert not f.evaluate(job("Engineer", location="Austin, TX", employment_type="Part-Time"))


def test_no_include_keywords_matches_everything_not_excluded():
    f = JobFilter(exclude_keywords=["intern"])
    assert f.evaluate(job("Anything"))
    assert not f.evaluate(job("Summer Intern"))


def test_could_match_prefilter(default_filter):
    assert default_filter.could_match(job("Software Engineer"))
    assert not default_filter.could_match(job("Senior Software Engineer"))
    assert not default_filter.could_match(job("Recruiter"))
    desc = JobFilter(include_keywords=["python"], match_on=["title", "description"])
    assert desc.could_match(job("Recruiter"))  # description unknown yet


def test_unknown_field_rejected():
    with pytest.raises(ValueError):
        JobFilter(match_on=["salary"])
    with pytest.raises(ValueError):
        JobFilter.from_config({"include": ["x"]})


def test_continental_finance_config():
    from pathlib import Path

    from config import load_config

    cfg = load_config(Path(__file__).parent.parent / "config" / "companies.yaml")
    cf = next(c for c in cfg.companies if c.name == "Continental Finance")
    f = cf.filter
    assert f.evaluate(job("Mid/Senior Software Developer (.NET Core / React / AWS)"))
    assert f.evaluate(job("Machine Learning Engineer"))
    assert f.evaluate(job("AI Platform Engineer"))
    assert not f.evaluate(job("Grand Bank - Chief Compliance Officer"))
    assert not f.evaluate(job("Director of Data Engineering"))
    assert not f.evaluate(job("Software Engineering Manager"))
    assert not f.evaluate(job("Compliance Analyst"))
