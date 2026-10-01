"""The packaged app's first-run setup: files, profile, the AI prompt, importing companies, Gmail."""

import io
import json

import httpx
import pytest
import yaml

from role_radar import cli, onboarding
from role_radar.config import load_config, profile_path
from role_radar.filters import JobFilter, compile_keyword
from role_radar.http_client import HttpClient
from tests.conftest import fixture_json, job

DIRECTORY = {
    onboarding._key("Stripe"): {"name": "Stripe", "url": "https://job-boards.greenhouse.io/stripe", "ats": "greenhouse"},
    onboarding._key("Acme Corp"): {"name": "Acme Corp", "url": "https://acme.wd5.myworkdayjobs.com/External", "options": {"x": 1}},
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("ROLE_RADAR_PROFILE", raising=False)
    path = tmp_path / "Role Radar" / "companies.yaml"
    onboarding.init(path)
    return path


@pytest.fixture
def boards(monkeypatch):
    """Job boards answering from fixtures: Greenhouse "good" lists jobs, anything else is 404."""
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if "boards/good/" in request.url.path:
            return httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
        return httpx.Response(404)

    monkeypatch.setattr(onboarding, "HttpClient", lambda s, **kw: HttpClient(s, transport=httpx.MockTransport(handler), **kw))
    return requested


def test_init_writes_a_companies_file_and_a_mac_only_profile(config):
    assert config.read_text().startswith(onboarding.GENERATED)
    loaded = load_config(config, profile_path(config))
    assert (loaded.runtime.storage, loaded.runtime.secrets, loaded.companies) == ("sqlite", "keychain", [])
    assert loaded.settings.digest_interval_minutes == 10
    state = onboarding.show(config)
    assert not state["ready"] and state["roles"] == [] and "senior" in state["exclude"]
    onboarding.init(config)  # again: keeps what's there


def test_profile_saves_what_they_look_for(config):
    onboarding.save_profile(config, ["data analyst", " analytics engineer ", ""], ["senior"], ["Toronto", "Remote"], 3)
    state = onboarding.show(config)
    assert (state["roles"], state["locations"], state["max_experience_years"]) == (["data analyst", "analytics engineer"],
                                                                                   ["Toronto", "Remote"], 3)
    with pytest.raises(ValueError, match="at least one job title"):
        onboarding.save_profile(config, [], [], [], None)
    with pytest.raises(ValueError, match="between 0 and 30"):
        onboarding.save_profile(config, ["x"], [], [], 40)


TECH_LIST = """companies:
  - {name: MathWorks, url: https://jobs.lever.co/mathworks, countries: [US, IN]}
  - {name: Flipkart Labs, url: https://jobs.lever.co/flipkart, countries: [IN]}
  - {name: Canva, url: https://careers.smartrecruiters.com/Canva, countries: [AU], platform: smartrecruiters, enabled: false}
  - {name: Untagged Co, url: https://jobs.lever.co/untagged}
  - {name: US Only, url: https://jobs.lever.co/usonly, countries: [US], filters: {include_keywords: [theirs]}}
"""


@pytest.fixture
def lists(tmp_path, monkeypatch):
    folder = tmp_path / "lists"
    folder.mkdir()
    (folder / "tech.yaml").write_text(TECH_LIST)
    monkeypatch.setenv("ROLE_RADAR_LISTS", str(folder))
    return folder


def test_a_profession_brings_its_titles_and_company_list(config, lists):
    state = onboarding.show(config)
    assert state["profession"] is None and state["companies"] == 0
    assert [p["name"] for p in state["professions"]] == ["Tech", "Accounting & Finance", "Healthcare"]
    onboarding.save_profession(config, "tech")
    state = onboarding.show(config)
    assert state["profession"] == "tech" and state["roles"] == onboarding.PROFESSIONS["tech"]["titles"]
    assert "distinguished" in state["exclude"]
    assert state["companies"] == 4  # the list's enabled companies; Canva is switched off
    loaded = load_config(config, profile_path(config))
    assert [c.name for c in loaded.companies][:2] == ["MathWorks", "Flipkart Labs"]
    assert loaded.companies[-1].filter.include_keywords == onboarding.PROFESSIONS["tech"]["titles"]  # not the list's own


def test_countries_choose_the_companies_and_cities_the_alerts(config, lists):
    onboarding.save_profession(config, "tech")
    onboarding.save_profile(config, ["software engineer"], ["senior"], [], 2, countries=["in"])
    state = onboarding.show(config)
    assert (state["countries"], state["cities"], state["profession"]) == (["IN"], [], "tech")
    assert state["companies"] == 3  # MathWorks, Flipkart Labs, and Untagged Co (always checked)
    loaded = load_config(config, profile_path(config))
    assert [c.name for c in loaded.companies if c.checked] == ["MathWorks", "Flipkart Labs", "Untagged Co"]
    india = loaded.companies[0].filter
    assert india.evaluate(job("Software Engineer", location="Pune, IN"))
    assert india.evaluate(job("Software Engineer", location="Remote"))
    assert not india.evaluate(job("Software Engineer", location="Indianapolis, IN"))
    onboarding.save_profile(config, ["software engineer"], [], ["Bengaluru"], 2, countries=["IN", "US"])
    state = onboarding.show(config)
    assert (state["countries"], state["cities"], state["companies"]) == (["US", "IN"], ["Bengaluru"], 4)
    city = load_config(config, profile_path(config)).companies[0].filter
    assert city.evaluate(job("Software Engineer", location="Bengaluru, India"))
    assert not city.evaluate(job("Software Engineer", location="Pune, India"))
    with pytest.raises(ValueError, match="at least one country"):
        onboarding.save_profile(config, ["x"], [], [], None, countries=["FR"])


def test_setup_counts_the_companies_each_choice_of_countries_tracks(config, lists):
    onboarding.save_profession(config, "tech")
    state = onboarding.show(config)
    assert state["companies_for"]["US"] == 3  # MathWorks, US Only, and Untagged Co (tracked everywhere)
    assert state["companies_for"]["IN"] == 3 and state["companies_for"]["US+CA+AU+IN"] == 4
    assert state["companies_by_country"] == {"US": 2, "CA": 0, "AU": 0, "IN": 2}
    assert state["companies_untagged"] == 1


def test_a_profession_is_ready_only_once_countries_are_picked(config, lists, monkeypatch):
    from role_radar import keychain

    monkeypatch.setattr(keychain, "read_all", lambda: {"EMAIL_TO": "me@gmail.com", "SMTP_PASSWORD": "x"})
    onboarding.save_profession(config, "tech")
    assert onboarding.show(config)["ready"] is False  # titles, companies and email, but no countries yet
    onboarding.save_profile(config, ["software engineer"], [], [], 2, countries=["US"])
    assert onboarding.show(config)["ready"] is True


def test_non_target_roles_and_education(config, lists):
    onboarding.save_profession(config, "tech")
    state = onboarding.show(config)
    tech = next(p for p in state["professions"] if p["id"] == "tech")
    assert tech["skip_groups"][0] == {"name": "Senior levels",
                                      "titles": ["senior", "sr", "staff", "principal", "distinguished", "lead", "manager"]}
    assert state["exclude"] == [w for group in tech["skip_groups"] for w in group["titles"]]  # all ticked, as boxes
    assert state["education"] is None
    # Untick Senior and Lead, add a word of their own: boxes save as their rules.
    words = [w for w in state["exclude"] if w not in ("senior", "lead")] + ["intern"]
    onboarding.save_profile(config, ["software engineer", "technical program manager"], words, [], 2,
                            countries=["US"], education="masters")
    state = onboarding.show(config)
    assert state["exclude"] == words and state["education"] == "masters"
    rules = load_config(config, profile_path(config)).companies[0].filter
    assert rules.education == "masters"
    austin = lambda title: job(title, location="Austin, TX")  # noqa: E731
    assert rules.evaluate(austin("Senior Software Engineer")) and rules.evaluate(austin("Lead Software Engineer"))
    assert not rules.evaluate(austin("Staff Software Engineer")) and not rules.evaluate(austin("Software Engineer Intern"))
    assert rules.evaluate(austin("Technical Program Manager"))  # "manager" spares program and product managers
    assert not rules.evaluate(austin("Software Engineering Manager"))
    # Pages that don't ask for education keep the saved one.
    onboarding.save_profile(config, ["software engineer"], words, [], 2, countries=["US"])
    assert onboarding.show(config)["education"] == "masters"
    with pytest.raises(ValueError, match="education must be one of"):
        onboarding.save_profile(config, ["software engineer"], [], [], 2, countries=["US"], education="diploma")
    onboarding.save_profession(config, "healthcare")
    assert "staff" not in onboarding.show(config)["exclude"]  # Staff Nurse is an entry-level title


def test_setups_examples_are_what_the_rules_do(config, lists):
    """Setup's ⓘ explains both lists with examples: each must be what the rules do, with only the boxes it
    names ticked and with every box ticked."""
    for pid, p in onboarding.PROFESSIONS.items():
        everything = JobFilter(include_keywords=p["titles"], exclude_keywords=p["exclude"])
        title, jobs = p["examples"]["target"]
        assert title in p["titles"], pid
        for name in jobs:
            assert compile_keyword(title).search(name) and everything.evaluate(job(name)), (pid, name)
        targets, words, reach, stopped = p["examples"]["non_target"]
        assert set(targets) <= set(p["titles"]) and set(words) <= set(p["patterns"]), pid
        named = JobFilter(include_keywords=targets, exclude_keywords=[p["patterns"][w] for w in words])
        for rules in (named, everything):
            assert all(rules.evaluate(job(name)) for name in reach), pid
            assert not any(rules.evaluate(job(name)) for name in stopped), pid
        assert not any(named.evaluate(job(name)).reason == "no include keyword matched" for name in stopped), pid
    tech = next(p for p in onboarding.show(config)["professions"] if p["id"] == "tech")
    assert tech["examples"]["non_target"] == {
        "targets": ["product manager", "software engineering"], "words": ["manager"],
        "reach": ["Product Manager Intern", "Software Engineering Intern"],
        "stopped": ["Software Engineering Manager", "Product Manager - Engineering Manager"]}


def test_older_rules_for_staff_and_manager_show_as_their_boxes(config, lists):
    """Setups before target titles shielded their words saved staff and manager as longer rules: still ticked."""
    onboarding.save_profession(config, "tech")
    path = profile_path(config)
    header = "".join(line for line in path.read_text().splitlines(keepends=True) if line.startswith("#"))
    raw = yaml.safe_load(path.read_text())
    old = {"staff": r"re:^(?!.*\b(?:member|associate)\b.*\btechnical[\s/_-]+staff\b).*\bstaff\b",
           "manager": r"re:^(?!.*\b(?:technical[\s/_-]+program|product)[\s/_-]+manager\b).*\bmanagers?\b"}
    labels = onboarding.PROFESSIONS["tech"]["labels"]
    raw["filters"]["exclude_keywords"] = [old.get(labels.get(r, r), r) for r in raw["filters"]["exclude_keywords"]]
    path.write_text(header + yaml.safe_dump(raw, sort_keys=False))
    assert set(old.values()) <= set(load_config(config, path).companies[0].filter.exclude_keywords)
    state = onboarding.show(config)
    assert {"staff", "manager"} <= set(state["exclude"])
    onboarding.save_profile(config, state["roles"], state["exclude"], [], 2, countries=["US"])
    saved = load_config(config, profile_path(config)).companies[0].filter.exclude_keywords
    assert "staff" in saved and r"re:\bmanagers?\b" in saved


def test_switching_profession_starts_the_search_afresh(config, lists, monkeypatch):
    from datetime import datetime, timezone

    when = [datetime(2026, 9, 30, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(onboarding, "utcnow", lambda: when[0])
    fresh_start = lambda: load_config(config, profile_path(config)).settings.fresh_start_at  # noqa: E731
    assert fresh_start() is None
    onboarding.save_profession(config, "tech")
    assert fresh_start() == "2026-09-30T12:00:00Z"
    when[0] = when[0].replace(hour=13)
    onboarding.save_profession(config, "tech")  # the same one again
    onboarding.save_profile(config, ["software engineer"], ["senior"], [], 2, countries=["US"])
    assert fresh_start() == "2026-09-30T12:00:00Z"  # other Setup changes don't start afresh
    onboarding.save_profession(config, "healthcare")
    assert fresh_start() == "2026-09-30T13:00:00Z"


def test_a_new_profession_keeps_their_own_titles_and_companies(config, lists, boards):
    onboarding.save_profession(config, "tech")
    titles = onboarding.PROFESSIONS["tech"]["titles"][:3] + ["quantum whisperer"]
    onboarding.save_profile(config, titles, ["senior"], [], 2)
    onboarding.save_profession(config, "tech")  # the same one again: nothing changes
    assert onboarding.show(config)["roles"] == titles
    onboarding.import_companies(config, "- name: Good Co\n  url: https://job-boards.greenhouse.io/good\n", directory={})
    onboarding.save_profession(config, "healthcare")
    state = onboarding.show(config)
    assert state["roles"] == onboarding.PROFESSIONS["healthcare"]["titles"] + ["quantum whisperer"]
    assert "attending" in state["exclude"] and state["own_companies"] == 1
    assert [c.name for c in load_config(config, profile_path(config)).companies] == ["Good Co"]  # no healthcare list yet
    with pytest.raises(ValueError, match="pick one of"):
        onboarding.save_profession(config, "law")


def test_known_employers_come_from_the_lists(lists):
    known = onboarding.load_directory()
    assert set(known) == {onboarding._key(n) for n in ("MathWorks", "Flipkart Labs", "Untagged Co", "US Only")}
    assert "filters" not in known[onboarding._key("US Only")]


def test_setup_never_rewrites_a_hand_edited_file(config):
    profile_path(config).write_text("filters:\n  include_keywords: [mine]\n")
    with pytest.raises(onboarding.NotGenerated):
        onboarding.save_profile(config, ["x"], [], [], None)
    config.write_text("companies: []\n")
    with pytest.raises(onboarding.NotGenerated):
        onboarding.import_companies(config, "- name: Stripe\n", directory=DIRECTORY)


def test_the_prompt_carries_their_roles_level_and_places(config):
    with pytest.raises(ValueError, match="roles"):
        onboarding.prompt(config)
    onboarding.save_profile(config, ["data analyst"], [], ["Toronto"], 0)
    text = onboarding.prompt(config)
    assert "data analyst" in text and "Toronto" in text and "entry-level" in text
    assert "https://job-boards.greenhouse.io/COMPANY" in text and "- name: Company Name" in text


@pytest.mark.parametrize("answer, expected", [
    ("```yaml\n- name: Stripe\n  url: https://job-boards.greenhouse.io/stripe\n- name: Notion\n```",
     [("Stripe", "https://job-boards.greenhouse.io/stripe"), ("Notion", None)]),
    ("1. Stripe - https://job-boards.greenhouse.io/stripe\n2. Notion\n- Figma (https://jobs.ashbyhq.com/figma)\n",
     [("Stripe", "https://job-boards.greenhouse.io/stripe"), ("Notion", None), ("Figma", "https://jobs.ashbyhq.com/figma")]),
    ("- name: Stripe\n- name: stripe, inc.\n", [("Stripe", None)]),
])
def test_the_ais_answer_is_read_however_its_written(answer, expected):
    assert [(c.name, c.url) for c in onboarding.parse_candidates(answer)] == expected


def test_companies_come_from_the_directory_or_a_working_job_board(config, boards):
    answer = """- name: Stripe
  url: https://example.com/careers
- name: Good Co
  url: https://job-boards.greenhouse.io/good
- name: Broken Co
  url: https://job-boards.greenhouse.io/broken
- name: Custom Page Co
  url: https://custompage.example/careers
- name: Nameless Startup
- name: Acme Corp.
"""
    report = onboarding.import_companies(config, answer, directory=DIRECTORY)
    added = {a["name"]: a for a in report.added}
    assert added["Stripe"]["source"] == "known" and added["Acme Corp"]["source"] == "known"  # the directory's link wins
    assert added["Good Co"]["source"] == "checked" and added["Good Co"]["jobs"] > 0
    skipped = {s["name"]: s["reason"] for s in report.skipped}
    assert set(skipped) == {"Broken Co", "Custom Page Co", "Nameless Startup"}
    assert "didn't answer" in skipped["Broken Co"] and "can read" in skipped["Custom Page Co"]
    companies = load_config(config).companies
    assert [c.name for c in companies] == ["Stripe", "Acme Corp", "Good Co"] and report.total == 3
    assert companies[1].options == {"x": 1}
    assert not any("custompage" in url for url in boards)  # an unreadable page isn't even fetched

    again = onboarding.import_companies(config, "- name: Stripe\n- name: Good Co\n  url: https://job-boards.greenhouse.io/good/",
                                        directory=DIRECTORY)
    assert again.added == [] and len(again.skipped) == 2 and again.total == 3
    replaced = onboarding.import_companies(config, "- name: Acme Corp\n", replace=True, directory=DIRECTORY)
    assert replaced.total == 1 and [c.name for c in load_config(config).companies] == ["Acme Corp"]


def test_gmail_settings_go_in_the_keychain(monkeypatch):
    from role_radar import keychain

    saved = {}
    monkeypatch.setattr(keychain, "write", lambda name, value=None: saved.__setitem__(name, value))
    monkeypatch.setattr(keychain, "read_all", lambda: dict(saved))
    onboarding.save_email(" me@gmail.com ", "abcd efgh ijkl mnop")
    assert saved["SMTP_PASSWORD"] == "abcdefghijklmnop" and saved["EMAIL_TO"] == saved["SMTP_USERNAME"] == "me@gmail.com"
    assert saved["SMTP_HOST"] == "smtp.gmail.com" and saved["SMTP_PORT"] == "587"
    with pytest.raises(ValueError, match="16 letters"):
        onboarding.save_email("me@gmail.com", "my real password")
    with pytest.raises(ValueError, match="email address"):
        onboarding.save_email("me at gmail", "abcdefghijklmnop")


def test_alerts_can_also_go_to_friends(monkeypatch):
    from role_radar import keychain

    saved = {}
    monkeypatch.setattr(keychain, "write", lambda name, value=None: saved.__setitem__(name, value))
    monkeypatch.setattr(keychain, "read", saved.get)
    monkeypatch.setattr(keychain, "read_all", lambda: dict(saved))
    with pytest.raises(ValueError, match="Gmail address and app password first"):
        onboarding.save_recipients(["friend@example.com"])
    onboarding.save_email("me@gmail.com", "abcdefghijklmnop")
    onboarding.save_recipients([" friend@example.com", "", "Friend@Example.com", "ME@gmail.com", "me@school.edu"])
    assert saved["EMAIL_TO"] == "me@gmail.com,friend@example.com,me@school.edu"  # the sender always, everyone once
    onboarding.save_email("new@gmail.com", "abcdefghijklmnop")  # a new Gmail keeps the list
    assert saved["EMAIL_TO"] == "new@gmail.com,friend@example.com,me@school.edu"
    assert onboarding._also(saved) == ["friend@example.com", "me@school.edu"]
    with pytest.raises(ValueError, match="email address"):
        onboarding.save_recipients(["a@example.com, b@example.com"])  # one per line, never a list in one
    onboarding.save_recipients([])
    assert saved["EMAIL_TO"] == "new@gmail.com"


def test_a_discord_webhook_goes_in_the_keychain(config, lists, monkeypatch):
    from role_radar import keychain

    saved = {}
    monkeypatch.setattr(keychain, "write", lambda name, value=None: saved.__setitem__(name, value))
    monkeypatch.setattr(keychain, "read_all", lambda: dict(saved))
    onboarding.save_profession(config, "tech")
    onboarding.save_profile(config, ["software engineer"], [], [], 2, countries=["US"])
    assert onboarding.show(config)["ready"] is True  # alerts are optional
    url = "https://discord.com/api/webhooks/123456789/abc-DEF_123"
    onboarding.save_discord(f" {url} ")
    assert saved == {"DISCORD_WEBHOOK_URL": url}
    state = onboarding.show(config)
    assert (state["discord_ready"], state["email_ready"], state["ready"]) == (True, False, True)
    with pytest.raises(ValueError, match="isn't a Discord webhook"):
        onboarding.save_discord("https://example.com/api/webhooks/1/x")


def test_keychain_values_go_to_security_on_stdin(monkeypatch):
    from role_radar import keychain

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        found = args[1] == "find-generic-password"
        return type("Done", (), {"returncode": 0, "stdout": "abcdefghijklmnop\n" if found else "", "stderr": ""})()

    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    monkeypatch.setattr(keychain.subprocess, "run", run)
    keychain.write("SMTP_PASSWORD", "abcdefghijklmnop")
    (args, stdin), _ = calls
    assert args == ["security", "-i"] and '-w "abcdefghijklmnop"' in stdin and "abcdefghijklmnop" not in " ".join(args)
    with pytest.raises(ValueError, match="quotes"):
        keychain.write("SMTP_PASSWORD", 'a"b')


def test_setup_command_runs_the_whole_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ROLE_RADAR_PROFILE", raising=False)
    monkeypatch.setattr(onboarding, "load_directory", lambda path=None: DIRECTORY)
    monkeypatch.setattr("role_radar.keychain.read_all", lambda: {"EMAIL_TO": "me@gmail.com", "SMTP_PASSWORD": "x"})
    config = str(tmp_path / "companies.yaml")

    def run(*args, stdin=""):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert cli.main(["setup", *args, "--config", config]) == 0
        return capsys.readouterr().out

    assert json.loads(run("init"))["ready"] is False
    run("profile", stdin=json.dumps({"roles": ["software engineer"], "exclude": ["senior"], "max_experience_years": 2}))
    assert "software engineer" in run("prompt")
    report = json.loads(run("companies", "--no-check", stdin="- name: Stripe\n"))
    assert [a["name"] for a in report["added"]] == ["Stripe"]
    state = json.loads(run("show"))
    assert state["ready"] and state["companies"] == 1 and state["email"] == "me@gmail.com"


# -- the Companies page: search, turn off, add their own ---------------------------------------


def test_finding_companies_says_which_are_tracked_and_why_not(config, lists):
    onboarding.save_profession(config, "tech")
    onboarding.save_profile(config, ["software engineer"], ["senior"], [], 2, countries=["IN"])
    found = onboarding.find_companies(config)
    assert found["total"] == 5 and [r["name"] for r in found["results"]] == [
        "Canva", "Flipkart Labs", "MathWorks", "Untagged Co", "US Only"]
    why = {r["name"]: (r["tracked"], r["why"]) for r in found["results"]}
    assert why["MathWorks"] == (True, None) and why["Untagged Co"] == (True, None)
    assert why["Canva"] == (False, "Role Radar can't read its job site yet")
    assert [r["name"] for r in found["results"] if not r["readable"]] == ["Canva"]
    assert why["US Only"] == (False, "it doesn't post jobs in your countries")
    assert [r["name"] for r in onboarding.find_companies(config, "labs")["results"]] == ["Flipkart Labs"]
    assert [r["name"] for r in onboarding.find_companies(config, "U")["results"]][:2] == ["Untagged Co", "US Only"]  # starts with it first
    assert [r["name"] for r in onboarding.find_companies(config, "lever math")["results"]] == ["MathWorks"]  # by its job site too
    page = onboarding.find_companies(config, "", limit=2, offset=2)
    assert page["total"] == 5 and [r["name"] for r in page["results"]] == ["MathWorks", "Untagged Co"]


def test_a_company_turned_off_is_never_checked_until_turned_back_on(config, lists, monkeypatch):
    onboarding.save_profession(config, "tech")
    onboarding.save_profile(config, ["software engineer"], ["senior"], [], 2, countries=["IN"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"names": ["MathWorks"], "tracked": False})))
    assert cli.main(["setup", "track", "--config", str(config)]) == 0
    assert onboarding.show(config)["companies"] == 2
    assert "MathWorks" not in [c.name for c in load_config(config, profile_path(config)).companies]
    (row,) = onboarding.find_companies(config, "mathworks")["results"]
    assert (row["off"], row["tracked"], row["why"]) == (True, False, "you turned it off")
    onboarding.save_profile(config, ["software engineer"], ["senior"], [], 2, countries=["IN", "US"])  # kept
    assert onboarding.find_companies(config, "mathworks")["results"][0]["off"]
    assert [r["name"] for r in onboarding.find_companies(config, which="off")["results"]] == ["MathWorks"]
    onboarding.set_tracked(config, ["MathWorks"], True)
    assert onboarding.find_companies(config, "mathworks")["results"][0]["tracked"]
    assert onboarding.find_companies(config)["untracked"] == 0


def test_adding_a_company_of_their_own_reads_its_board_first(config, lists, boards):
    onboarding.save_profession(config, "tech")
    added = onboarding.add_company(config, "Good Co", "https://job-boards.greenhouse.io/good")
    assert added["status"] == "added" and added["jobs"] == 2
    (row,) = onboarding.find_companies(config, "good")["results"]
    assert row["own"] and row["tracked"] and row["site"] == "greenhouse"
    assert onboarding.add_company(config, "good co", "https://example.com/careers")["status"] == "listed"

    onboarding.set_tracked(config, ["MathWorks"], False)
    again = onboarding.add_company(config, "MathWorks", "https://jobs.lever.co/mathworks")
    assert again == {"status": "listed", "name": "MathWorks", "turned_on": True, "why": None}
    assert onboarding.find_companies(config, "mathworks")["results"][0]["tracked"]

    failed = onboarding.add_company(config, "Gone Co", "https://job-boards.greenhouse.io/gone")
    assert failed["status"] == "failed" and "didn't answer" in failed["reason"]
    custom = onboarding.add_company(config, "Own Site", "https://www.ownsite.example/careers")
    assert custom["status"] == "failed" and "can read" in custom["reason"]
    with pytest.raises(ValueError, match="https://"):
        onboarding.add_company(config, "No Link", "ownsite careers page")


def test_adding_a_company_by_name_says_whether_it_can_be_tracked(config, lists, boards):
    onboarding.save_profession(config, "tech")
    onboarding.save_profile(config, ["software engineer"], [], [], 2, countries=["IN"])
    assert onboarding.add_company(config, "mathworks") == {"status": "listed", "name": "MathWorks", "turned_on": False,
                                                           "why": None}
    assert onboarding.add_company(config, "US Only")["why"] == "it doesn't post jobs in your countries"
    canva = onboarding.add_company(config, "Canva")  # on the list, on a job site Role Radar can't read yet
    assert canva["status"] == "failed" and canva["url"] and "can't read" in canva["reason"]
    assert onboarding.add_company(config, "Nowhere Co") == {"status": "failed", "name": "Nowhere Co",
                                                            "reason": "no careers page to read"}
    added = onboarding.add_company(config, "Good Co", "job-boards.greenhouse.io/good")  # https:// assumed
    assert added["status"] == "added"
    assert onboarding.find_companies(config, "good")["results"][0]["url"] == "https://job-boards.greenhouse.io/good"
