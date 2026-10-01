import pytest

from role_radar.filters import JobFilter, compile_keyword
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


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Product Manager", True),  # "manager" is part of the target title that let it in
        ("Technical Product Manager, AI", True),
        ("Member of Technical Staff", True),
        ("Software Engineering Manager", False),  # let in by "software engineering"; "manager" is outside it
        ("Senior Product Manager", False),  # "senior" is outside "product manager"
        ("Staff Software Engineer", False),
        ("Product Manager / Engineering Manager", False),  # the second "manager" isn't part of a target title
    ],
)
def test_a_target_title_shields_the_non_target_words_inside_it(title, expected):
    f = JobFilter(include_keywords=["product manager", "software engineering", "software engineer", "member of technical staff"],
                  exclude_keywords=["senior", "staff", r"re:\bmanagers?\b"])
    assert f.evaluate(job(title)).matched is expected
    assert f.could_match(job(title)) is expected


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


def test_description_filters_are_rejected():
    with pytest.raises(ValueError, match="keywords don.t match job descriptions"):
        JobFilter(include_keywords=["kubernetes"], match_on=["title", "description"])
    with pytest.raises(ValueError, match="keywords don.t match job descriptions"):
        JobFilter(exclude_keywords=["clearance"], exclude_on=["description"])


def test_config_with_description_filter_fails_clearly(tmp_path):
    from role_radar.config import load_config

    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n"
        "  - name: Ashby\n"
        "    url: https://jobs.ashbyhq.com/ashby\n"
        "    enabled: false\n"  # disabled companies are validated too
        "    filters:\n"
        "      match_on: [title, description]\n"
    )
    with pytest.raises(ValueError, match=r"companies\[0\] \(Ashby\): .*descriptions"):
        load_config(path)


def test_fields_used():
    assert JobFilter().fields_used() == set()
    assert JobFilter(include_keywords=["engineer"]).fields_used() == {"title"}
    both = JobFilter(include_keywords=["engineer"], locations=["Remote"], employment_types=["full-time"])
    assert both.fields_used() == {"title", "location", "employment_type"}
    assert JobFilter(exclude_keywords=["intern"], exclude_on=["title", "department"]).fields_used() == {"title", "department"}


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
    located = JobFilter(include_keywords=["engineer"], locations=["New York"])
    multi = job("Data Engineer", location="2 Locations")
    assert not located.could_match(multi)  # "2 Locations" taken at face value
    assert located.could_match(multi, unknown={"location"})  # the detail page may say New York
    assert located.could_match(job("Data Engineer", location=None))  # empty fields count as unknown
    assert not located.could_match(job("Recruiter", location="2 Locations"), unknown={"location"})


def test_unknown_field_rejected():
    with pytest.raises(ValueError):
        JobFilter(match_on=["salary"])
    with pytest.raises(ValueError):
        JobFilter.from_config({"include": ["x"]})


def test_continental_finance_config():
    from pathlib import Path

    from role_radar.config import load_config

    config_dir = Path(__file__).parent.parent / "config"
    cfg = load_config(config_dir / "companies.yaml", config_dir / "profile.example.yaml")
    cf = next(c for c in cfg.companies if c.name == "Continental Finance")
    f = cf.filter
    assert f.evaluate(job("Mid/Senior Software Developer (.NET Core / React / AWS)"))
    assert f.evaluate(job("Machine Learning Engineer"))
    assert f.evaluate(job("AI Platform Engineer"))
    assert not f.evaluate(job("Grand Bank - Chief Compliance Officer"))
    assert not f.evaluate(job("Director of Data Engineering"))
    assert not f.evaluate(job("Software Engineering Manager"))
    assert not f.evaluate(job("Compliance Analyst"))
