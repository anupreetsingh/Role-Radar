"""The packaged app's first-run setup: files, profile, the AI prompt, importing companies, Gmail."""

import io
import json

import httpx
import pytest

from role_radar import cli, onboarding
from role_radar.config import load_config, profile_path
from role_radar.http_client import HttpClient
from tests.conftest import fixture_json

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
    with pytest.raises(ValueError, match="at least one role"):
        onboarding.save_profile(config, [], [], [], None)
    with pytest.raises(ValueError, match="between 0 and 30"):
        onboarding.save_profile(config, ["x"], [], [], 40)


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
    onboarding.save_email(" me@gmail.com ", "abcd efgh ijkl mnop")
    assert saved["SMTP_PASSWORD"] == "abcdefghijklmnop" and saved["EMAIL_TO"] == saved["SMTP_USERNAME"] == "me@gmail.com"
    assert saved["SMTP_HOST"] == "smtp.gmail.com" and saved["SMTP_PORT"] == "587"
    with pytest.raises(ValueError, match="16 letters"):
        onboarding.save_email("me@gmail.com", "my real password")
    with pytest.raises(ValueError, match="email address"):
        onboarding.save_email("me at gmail", "abcdefghijklmnop")


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
