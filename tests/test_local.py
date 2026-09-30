"""Running on one Mac: the profile beside the companies file, SQLite state, Keychain secrets, and backing up the profile."""

import pytest
import yaml

from role_radar import cli, ui
from role_radar.backends import ConfigSource, NotifierSource, open_backend, resolve_runtime
from role_radar.config import RuntimeSettings, load_config, profile_path, split_profile
from role_radar.instance import InstanceLock
from role_radar.sqlite import SqliteStateStore
from role_radar.storage import CompanyMeta
from tests.conftest import job
from tests.test_dynamo import add_jobs

COMPANIES = """
settings:
  check_interval_minutes: 15
companies:
  - name: Acme
    url: https://jobs.lever.co/acme
  - name: Beta
    url: https://jobs.lever.co/beta
    filters:
      exclude_keywords: [intern]
"""

PROFILE = """
runtime:
  storage: sqlite
  secrets: keychain
filters:
  include_keywords: [engineer]
  exclude_keywords: [senior]
  max_experience_years: 2
settings:
  check_interval_minutes: 30
"""


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.delenv("ROLE_RADAR_PROFILE", raising=False)
    companies = tmp_path / "companies.yaml"
    companies.write_text(COMPANIES)
    (tmp_path / "profile.yaml").write_text(PROFILE)
    return companies


def test_the_profile_supplies_runtime_filters_and_settings(files):
    config = load_config(files, profile_path(files))
    acme, beta = config.companies
    assert (config.runtime.storage, config.runtime.secrets, config.settings.check_interval_minutes) == ("sqlite", "keychain", 30)
    assert acme.filter.include_keywords == ["engineer"] and acme.filter.max_experience_years == 2
    assert acme.filter.evaluate(job("Software Engineer")) and not acme.filter.evaluate(job("Senior Software Engineer"))
    # A company's own filters still replace the profile's, key by key.
    assert beta.filter.exclude_keywords == ["intern"] and beta.filter.include_keywords == ["engineer"]
    assert resolve_runtime(files, env={}).storage == "sqlite"
    assert load_config(files).companies[0].filter.include_keywords == []  # the companies file alone


def test_a_profile_with_other_sections_is_refused(files):
    files.with_name("profile.yaml").write_text("companies: []\n")
    with pytest.raises(ValueError, match="unknown sections"):
        load_config(files, profile_path(files))


def test_a_config_with_no_roles_isnt_used(files):
    files.with_name("profile.yaml").unlink()
    with pytest.raises(ValueError, match="no roles to look for"):
        ConfigSource(RuntimeSettings(), files).load()


def test_the_profile_can_live_elsewhere(files, tmp_path, monkeypatch):
    elsewhere = tmp_path / "mine.yaml"
    elsewhere.write_text("filters:\n  include_keywords: [designer]\n")
    monkeypatch.setenv("ROLE_RADAR_PROFILE", str(elsewhere))
    assert profile_path(files) == elsewhere
    assert load_config(files, profile_path(files)).companies[0].filter.include_keywords == ["designer"]


def test_picked_countries_choose_which_companies_are_checked(files):
    files.write_text("""
companies:
  - name: MathWorks
    url: https://jobs.lever.co/mathworks
    countries: [us, IN]
  - name: Flipkart
    url: https://jobs.lever.co/flipkart
    countries: [IN]
  - name: Canva
    url: https://jobs.lever.co/canva
    countries: [AU]
  - name: Freshworks
    url: https://jobs.smartrecruiters.com/Freshworks
    countries: [IN, US]
    platform: smartrecruiters
    enabled: false
  - name: My Own Pick
    url: https://jobs.lever.co/mine
""")
    checked = lambda config: [c.name for c in config.companies if c.checked]  # noqa: E731
    # No countries picked: every enabled company, as before.
    assert checked(load_config(files, profile_path(files))) == ["MathWorks", "Flipkart", "Canva", "My Own Pick"]
    files.with_name("profile.yaml").write_text(PROFILE + "  countries: [US]\n")
    config = load_config(files, profile_path(files))
    assert config.companies[0].countries == ["US", "IN"] and config.companies[3].platform == "smartrecruiters"
    # A company with no countries of its own (one the person added) is always checked.
    assert checked(config) == ["MathWorks", "My Own Pick"]
    files.with_name("profile.yaml").write_text(PROFILE + "  countries: [IN, AU]\n")
    assert checked(load_config(files, profile_path(files))) == ["MathWorks", "Flipkart", "Canva", "My Own Pick"]


@pytest.mark.parametrize("line", ["countries: US", "countries: [USA]", "in_countries: false"])
def test_company_countries_are_checked(files, line):
    files.write_text(f"companies:\n  - name: Acme\n    url: https://jobs.lever.co/acme\n    {line}\n")
    with pytest.raises(ValueError, match="country code|unknown keys"):
        load_config(files)


def test_the_profession_must_be_one_setup_offers(files):
    files.with_name("profile.yaml").write_text(PROFILE + "  profession: law\n")
    with pytest.raises(ValueError, match="settings.profession must be one of tech, accounting, healthcare"):
        load_config(files, profile_path(files))


def test_split_profile_restores_what_config_push_combined(files):
    from role_radar.config import combined

    raw = combined(files, profile_path(files))
    assert split_profile(raw) == {"runtime": {"storage": "sqlite", "secrets": "keychain"},
                                  "filters": {"include_keywords": ["engineer"], "exclude_keywords": ["senior"],
                                              "max_experience_years": 2}}


# -- SQLite state ----------------------------------------------------------------------


def test_the_checker_reads_a_changed_profile_at_once(files, tmp_path):
    """Setup saves the profile while the checker runs: its next pass uses it, not one five minutes on."""
    import os

    from role_radar.runner import Runner

    backend = open_backend(RuntimeSettings(storage="sqlite", state_file=str(tmp_path / "state.db")), "laptop:mac")
    runner = Runner(ConfigSource(RuntimeSettings(), files), backend, [], clock=lambda: 1000.0)
    assert runner.config().companies[0].filter.exclude_keywords == ["senior"]
    profile = profile_path(files)
    profile.write_text(PROFILE.replace("[senior]", "[senior, intern]"))
    stat = profile.stat()
    os.utime(profile, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert runner.config().companies[0].filter.exclude_keywords == ["senior", "intern"]


def test_sqlite_backend_keeps_state_on_this_mac(tmp_path):
    backend = open_backend(RuntimeSettings(storage="sqlite", state_file=str(tmp_path / "state.db")), "laptop:mac")
    assert isinstance(backend.store, SqliteStateStore) and backend.lease.acquire(180)
    assert RuntimeSettings(storage="sqlite").state_path.name == "state.db"


def test_two_processes_can_write_without_losing_each_others_changes(tmp_path):
    """The checker saves companies while the app's commands flip switches and skip matches."""
    checker, app = SqliteStateStore(tmp_path / "state.db"), SqliteStateStore(tmp_path / "state.db")
    record = checker.load_company("Acme")
    first, second = add_jobs(record, "Software Engineer", "Data Engineer")
    record.meta = CompanyMeta(last_checked_at="2026-09-01T00:00:00Z")
    checker.save_company(record)

    assert app.mark_skipped("Acme", first.uid, True)
    app.save_switch("email", False)
    record.jobs[second.uid].location = "Remote"  # the checker saves again, from what it loaded before
    checker.save_company(record)

    rows = {m.uid: m for m in checker.load_queue()}
    assert rows[first.uid].skipped_at and rows[second.uid].location == "Remote"
    assert checker.load_switches() == {"email": False}
    assert checker.load_schedule()["Acme"].last_checked_at == "2026-09-01T00:00:00Z"


def test_sqlite_expires_old_rows_and_logs_alerts(tmp_path):
    from tests.conftest import Clock

    clock = Clock()
    store = SqliteStateStore(tmp_path / "state.db", clock)
    record = store.load_company("Acme")
    (sent,) = add_jobs(record, "Software Engineer")
    record.jobs[sent.uid].notified_at = "2026-09-01T00:05:00Z"
    record.alerted.append(sent.uid)
    store.save_company(record)
    (alert,) = store.recent_alerts()
    assert (alert["company"], alert["notified_at"], alert["by"]) == ("Acme", "2026-09-01T00:05:00Z", "laptop")
    store.record_stats("laptop", "2026-09-01T00", {"checked": 3})
    store.record_stats("laptop", "2026-09-01T00", {"checked": 2, "alerts": 1})
    assert store.load_stats("2026-09-01T00") == [{"hour": "2026-09-01T00", "runner": "laptop", "checked": 5, "alerts": 1}]
    clock.advance(31 * 86400)
    assert store.recent_alerts() == [] and store.load_stats("2026-09-01T00") == []


def test_migrate_copies_state_into_sqlite(tmp_path, capsys):
    from role_radar.storage import JsonStateStore, MonitorState

    source = JsonStateStore(tmp_path / "state.json")
    source.save(MonitorState(companies={"Acme": {}}, meta={"Acme": CompanyMeta(last_checked_at="2026-09-01T00:00:00Z")}))
    config = tmp_path / "companies.yaml"
    config.write_text(COMPANIES)
    target = tmp_path / "state.db"
    assert cli.main(["migrate", "--from", f"json:{tmp_path / 'state.json'}", "--to", f"sqlite:{target}",
                     "--config", str(config)]) == 0
    assert SqliteStateStore(target).load_schedule()["Acme"].last_checked_at == "2026-09-01T00:00:00Z"


# -- Keychain secrets, the menu bar, and runs beside the checker ---------------------------


def test_keychain_secrets_feed_the_alert_channels(monkeypatch):
    from role_radar import keychain

    monkeypatch.setattr(keychain, "read_all", lambda: {"DISCORD_WEBHOOK_URL": "https://discord.invalid/hook"})
    (channel,) = NotifierSource(RuntimeSettings(secrets="keychain"))()
    assert channel.name == "discord"
    monkeypatch.setattr(keychain, "read_all", lambda: {})
    assert NotifierSource(RuntimeSettings(secrets="keychain"))() == []  # nothing set: alerts wait, never the console


def test_keychain_uses_the_security_tool(monkeypatch):
    from role_radar import keychain

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        found = args[1] == "find-generic-password" and args[5] == "EMAIL_TO"
        return type("Done", (), {"returncode": 0 if found or args[1] != "find-generic-password" else keychain.NOT_FOUND,
                                 "stdout": "me@example.com\n" if found else "", "stderr": ""})()

    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    monkeypatch.setattr(keychain.subprocess, "run", run)
    assert keychain.read_all() == {"EMAIL_TO": "me@example.com"}
    keychain.write("SMTP_PASSWORD")  # no value: `security` asks on the terminal
    assert calls[-1][-2:] == ["Role Radar SMTP_PASSWORD", "-w"]
    with pytest.raises(ValueError, match="unknown setting"):
        keychain.write("PASSWORD", "x")


def test_without_aws_the_menu_never_shows_lambda_checking(tmp_path):
    backend = open_backend(RuntimeSettings(storage="sqlite", state_file=str(tmp_path / "state.db")), "switch")
    state = ui.snapshot(backend, lambda: None, storage="sqlite")
    assert (state["checking"], state["storage"]) == (None, "sqlite")
    assert ui.snapshot(backend, lambda: 123, storage="sqlite")["checking"] == "laptop"


def test_run_once_refuses_while_the_checker_runs_on_this_mac(files, monkeypatch):
    monkeypatch.setattr(InstanceLock, "running_pid", lambda self: 4242)
    assert cli.main(["run", "--once", "--config", str(files)]) == cli.EXIT_USAGE


# -- config push / pull ---------------------------------------------------------------


def test_config_push_uploads_the_combined_config_and_pull_restores_the_profile(files, fake_aws, monkeypatch):
    import boto3

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="role-radar-config")
    url = "s3://role-radar-config/companies.yaml"
    profile = files.with_name("profile.yaml")
    profile.write_text(PROFILE.replace("storage: sqlite\n  secrets: keychain",
                                       f"storage: sqlite\n  secrets: keychain\n  config_url: {url}\n  region: us-east-1"))
    monkeypatch.setattr(InstanceLock, "running_pid", lambda self: None)
    assert cli.main(["config", "push", "--config", str(files)]) == 0
    pushed = yaml.safe_load(boto3.client("s3").get_object(Bucket="role-radar-config", Key="companies.yaml")["Body"].read())
    assert pushed["defaults"]["filters"]["include_keywords"] == ["engineer"] and len(pushed["companies"]) == 2

    original = profile.read_text()
    profile.unlink()  # a new Mac
    assert cli.main(["config", "pull", "--url", url, "--config", str(files)]) == 0
    restored = load_config(files, profile)
    assert restored.companies[0].filter.include_keywords == ["engineer"] and restored.runtime.config_url == url
    assert cli.main(["config", "pull", "--url", url, "--config", str(files)]) == 0  # the same again: fine
    profile.write_text(original)
    assert cli.main(["config", "pull", "--url", url, "--config", str(files)]) == cli.EXIT_USAGE  # would replace yours
