"""The role-radar command line, end to end with mocked HTTP (and moto for AWS)."""

import os
import plistlib
import sys

import httpx
import pytest

from role_radar import cli, launchd, monitor
from role_radar.http_client import HttpClient
from role_radar.instance import InstanceLock
from role_radar.storage import JsonStateStore
from tests.conftest import fixture_json

CONFIG = """
runtime:
  storage: json
  state_file: {state}
settings:
  http: {{per_domain_delay: 0, respect_robots: false, max_retries: 0}}
companies:
  - name: Continental Finance
    url: https://contfinco.bamboohr.com/careers
    filters:
      include_keywords: [software developer, data engineer]
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A local config with JSON state in tmp_path, and every HTTP request answered from fixtures."""
    path = tmp_path / "companies.yaml"
    path.write_text(CONFIG.format(state=tmp_path / "state.json"))

    def handler(request):
        return httpx.Response(200, json=fixture_json("bamboohr_list.json"))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(monitor, "HttpClient", lambda s, **kw: HttpClient(s, transport=transport, **kw))
    for name in ("DISCORD_WEBHOOK_URL", "SMTP_HOST", "EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)  # alerts go to the console
    return path


def test_run_once_alerts_then_finds_nothing_due(config, capsys):
    assert cli.main(["run", "--once", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "Mid/Senior Software Developer" in out and "Data Engineer" in out
    state = JsonStateStore(config.parent / "state.json").load()
    assert len(state.companies["Continental Finance"]) == 4 and "Continental Finance" in state.meta

    assert cli.main(["run", "--once", "--config", str(config)]) == 0
    assert "NEW JOB" not in capsys.readouterr().out  # checked minutes ago: not due


def test_alert_switches_hold_matches_until_one_is_back_on(config, capsys):
    assert cli.main(["switch", "discord", "off", "--config", str(config)]) == 0
    assert cli.main(["switch", "email", "off", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "discord OFF, email OFF" in out and "Both alert channels are off" in out
    assert cli.main(["run", "--once", "--config", str(config)]) == 0
    assert "NEW JOB" not in capsys.readouterr().out

    assert cli.main(["switch", "email", "on", "--config", str(config)]) == 0  # the console has no switch of its own
    assert cli.main(["run", "--once", "--all", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "Mid/Senior Software Developer" in out and "Data Engineer" in out


def test_run_needs_once(config):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--config", str(config)])
    assert exc.value.code == 2


def test_dry_run_saves_nothing(config, capsys):
    assert cli.main(["run", "--once", "--dry-run", "--config", str(config)]) == 0
    assert "NEW JOB" in capsys.readouterr().out
    assert not (config.parent / "state.json").exists()


def test_status_counts_companies_not_saved_yet(config, capsys):
    assert cli.main(["status", "--config", str(config)]) == 0
    assert "1 not saved yet: waiting for a first successful check" in capsys.readouterr().out
    cli.main(["run", "--once", "--config", str(config)])
    capsys.readouterr()
    cli.main(["status", "--config", str(config)])
    assert "not saved yet" not in capsys.readouterr().out


def test_status_after_a_run(config, capsys):
    cli.main(["run", "--once", "--config", str(config)])
    capsys.readouterr()
    assert cli.main(["status", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "State:          JSON file" in out
    assert "Lease:          not needed" in out
    assert "role-radar start is not running" in out
    assert "Companies:      1 enabled, 0 due now, next due" in out
    assert "Continental Finance: Data Engineer" in out  # recent alerts
    assert "Last pass:      cli:" in out and "1 checked, 2 alert(s)" in out


def test_list_matches(config, capsys):
    assert cli.main(["list-matches", "--config", str(config)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and all(line.startswith("Continental Finance | ") for line in lines)


def test_missing_config_is_a_usage_error(tmp_path):
    assert cli.main(["status", "--config", str(tmp_path / "nope.yaml")]) == cli.EXIT_USAGE


def test_doctor_reports_missing_automation_and_channels_without_writing(config, capsys):
    assert cli.main(["doctor", "--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert "Local mode only" in out and "No delivery channels" in out
    assert not (config.parent / "state.json").exists()


def test_notification_test_uses_configured_channel_without_job_state(config, monkeypatch, capsys):
    from role_radar.notifications import DiscordNotifier

    sent = []

    async def send(self, jobs):
        sent.extend(jobs)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")
    monkeypatch.setattr(DiscordNotifier, "send", send)
    assert cli.main(["notifications", "test", "--config", str(config)]) == 0
    assert len(sent) == 1 and "[TEST]" in sent[0].title
    assert not (config.parent / "state.json").exists()


def test_notification_test_refuses_console_only(config):
    assert cli.main(["notifications", "test", "--config", str(config)]) == cli.EXIT_USAGE


def test_config_push_validates_then_uploads(config, fake_aws):
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="role-radar-config")
    text = config.read_text().replace("  storage: json\n", "  storage: json\n  config_url: s3://role-radar-config/companies.yaml\n")
    config.write_text(text)
    assert cli.main(["config", "push", "--config", str(config)]) == 0
    assert s3.get_object(Bucket="role-radar-config", Key="companies.yaml")["Body"].read().decode() == text

    broken = config.parent / "broken.yaml"
    broken.write_text(text.replace("include_keywords", "match_on: [description]\n      include_keywords"))
    assert cli.main(["config", "push", "--config", str(config), "--file", str(broken)]) == cli.EXIT_USAGE
    assert s3.get_object(Bucket="role-radar-config", Key="companies.yaml")["Body"].read().decode() == text


def test_config_push_needs_a_destination(config):
    assert cli.main(["config", "push", "--config", str(config)]) == cli.EXIT_USAGE


def test_migrate_json_to_dynamodb(config, table, capsys):
    cli.main(["run", "--once", "--config", str(config)])  # fill the JSON state
    source = f"json:{config.parent / 'state.json'}"
    target = f"dynamodb:{table[1]}"
    assert cli.main(["migrate", "--from", source, "--to", target, "--config", str(config)]) == 0
    assert "Copied 1 companies (4 jobs, 1 schedules)" in capsys.readouterr().out

    from role_radar.dynamo import DynamoStateStore

    migrated = DynamoStateStore(table[0], table[1]).load()
    assert migrated.to_dict()["companies"] == JsonStateStore(config.parent / "state.json").load().to_dict()["companies"]
    assert cli.main(["migrate", "--from", source, "--to", target, "--config", str(config)]) == 1  # already has state
    assert cli.main(["migrate", "--from", source, "--to", target, "--config", str(config), "--force"]) == 0


def test_migrate_waits_for_the_lease(config, table):
    from role_radar.dynamo import DynamoLease

    DynamoLease(table[0], table[1], "lambda").acquire(900)
    source = f"json:{config.parent / 'state.json'}"
    assert cli.main(["migrate", "--from", source, "--to", f"dynamodb:{table[1]}", "--config", str(config)]) == 4


@pytest.mark.skipif(sys.platform == "win32", reason="launchd is macOS's (Windows' checker: test_windows.py)")
def test_login_item_writes_an_agent_the_app_starts_without_keepalive(config, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launchd, "_launchctl", lambda *args, check=True: calls.append(args))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setenv("AWS_PROFILE", "role-radar")
    config.write_text(config.read_text().replace("  storage: json\n", "  storage: json\n  secrets: ssm:/role-radar/\n"))

    assert cli.main(["login-item", "on", "--config", str(config)]) == 0
    path = tmp_path / "Library" / "LaunchAgents" / "com.roleradar.start.plist"
    agent = plistlib.loads(path.read_bytes())
    assert agent["ProgramArguments"][:4] == [sys.executable, "-m", "role_radar", "start"]
    assert agent["ProgramArguments"][4:6] == ["--config", str(config.resolve())]
    assert agent["RunAtLoad"] is False and "KeepAlive" not in agent  # the menu bar app starts it
    assert agent["EnvironmentVariables"]["AWS_PROFILE"] == "role-radar"
    assert [c[0] for c in calls] == ["bootout", "bootstrap", "kickstart"]

    assert cli.main(["login-item", "off"]) == 0
    assert not path.exists() and calls[-1][0] == "bootout"


def test_switch_start_starts_the_checker_only_while_the_laptop_is_switched_on(config, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(launchd, "start", lambda: started.append(1) or True)
    monkeypatch.setattr(cli, "_wait_for_start", lambda: None)
    assert cli.main(["switch", "--start", "--json", "--config", str(config)]) == 0
    assert started == [1]
    assert cli.main(["switch", "laptop", "off", "--start", "--json", "--config", str(config)]) == 0
    assert cli.main(["switch", "--start", "--json", "--config", str(config)]) == 0
    assert started == [1]


def test_instance_lock_allows_one_start_and_finds_it(tmp_path):
    first, second = InstanceLock(tmp_path / "start.pid"), InstanceLock(tmp_path / "start.pid")
    assert first.acquire()
    assert not second.acquire()
    assert second.running_pid() == os.getpid()
    first.release()
    assert second.running_pid() is None and second.acquire()
    second.release()


def test_stop_when_nothing_runs(capsys):
    assert cli.main(["stop"]) == 0
    assert "isn't running" in capsys.readouterr().out


def test_default_config_path_prefers_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_RADAR_CONFIG_FILE", str(tmp_path / "mine.yaml"))
    assert cli.default_config_path() == tmp_path / "mine.yaml"


@pytest.mark.parametrize("seconds, text", [(5, "5s"), (125, "2m 05s"), (3 * 3600 + 60, "3h 01m"), (2 * 86400 + 7200, "2d 2h")])
def test_duration(seconds, text):
    assert cli.duration(seconds) == text


def test_start_keeps_trying_when_the_config_is_unreachable_at_login(config, monkeypatch):
    from role_radar import backends

    calls = []

    def offline(self):
        calls.append(1)
        raise ConnectionError("network is unreachable")

    async def serve(self, stop):  # the loop itself retries; here, just return
        return 0

    monkeypatch.setattr(backends.ConfigSource, "load", offline)
    monkeypatch.setattr(cli.Runner, "serve", serve)
    assert cli.main(["start", "--config", str(config)]) == 0  # didn't give up at startup
    assert calls == [1]


def test_login_item_refuses_secrets_from_the_shell(config, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(launchd, "_launchctl", lambda *args, check=True: pytest.fail("must not install"))
    assert cli.main(["login-item", "on", "--config", str(config)]) == cli.EXIT_USAGE  # secrets: env


def test_run_can_use_the_local_file_before_the_first_push(config, capsys):
    # config_url points at a bucket that doesn't exist; --local-config reads the file instead.
    config.write_text(config.read_text().replace("  storage: json\n", "  storage: json\n  config_url: s3://nowhere/companies.yaml\n"))
    assert cli.main(["run", "--once", "--baseline", "--local-config", "--config", str(config)]) == 0
    state = JsonStateStore(config.parent / "state.json").load()
    assert sum(bool(j.notified_at) for j in state.companies["Continental Finance"].values()) == 2  # recorded, not sent
    assert "NEW JOB" not in capsys.readouterr().out
