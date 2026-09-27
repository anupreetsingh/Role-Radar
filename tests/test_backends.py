"""Runtime settings, backend selection, config from S3 and secrets from SSM."""

import pytest

from role_radar import aws
from role_radar.backends import ConfigSource, NotifierSource, open_backend, resolve_runtime
from role_radar.config import RuntimeSettings, parse_config
from role_radar.lease import LocalLease
from role_radar.notifications import ConsoleNotifier, DiscordNotifier
from role_radar.storage import JsonStateStore

CONFIG = """
settings:
  check_interval_minutes: 15
companies:
  - name: Acme
    url: https://jobs.lever.co/acme
"""


def test_environment_overrides_the_runtime_section():
    yaml_side = RuntimeSettings(storage="json", table="from-yaml", region="eu-west-1")
    env = {"ROLE_RADAR_STORAGE": "dynamodb", "ROLE_RADAR_TABLE": "from-env", "AWS_REGION": "us-east-1"}
    resolved = yaml_side.with_env(env)
    assert (resolved.storage, resolved.table, resolved.region) == ("dynamodb", "from-env", "us-east-1")
    assert yaml_side.with_env({"ROLE_RADAR_REGION": "ap-south-1", "AWS_REGION": "us-east-1"}).region == "ap-south-1"
    assert yaml_side.with_env({}).table == "from-yaml"


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"storage": "sqlite"}, "json' or 'dynamodb"),
        ({"storage": "dynamodb"}, "no table"),
        ({"secrets": "vault"}, "ssm:/path/"),
        ({"secrets": "ssm:/role-radar"}, "ssm:/path/"),  # needs the trailing slash
        ({"config_url": "https://example.com/c.yaml"}, "s3://"),
    ],
)
def test_runtime_settings_are_validated(settings, message):
    with pytest.raises(ValueError, match=message):
        RuntimeSettings(**settings).with_env({})


def test_runtime_section_is_read_from_the_local_file(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(CONFIG + "runtime:\n  storage: dynamodb\n  table: role-radar\n  secrets: ssm:/role-radar/\n")
    runtime = resolve_runtime(path, env={})
    assert (runtime.storage, runtime.table, runtime.ssm_path) == ("dynamodb", "role-radar", "/role-radar/")
    assert resolve_runtime(tmp_path / "missing.yaml", env={}) == RuntimeSettings()
    with pytest.raises(ValueError, match="unknown keys"):
        parse_config(CONFIG + "runtime:\n  database: x\n")


def test_reserved_company_names_are_rejected():
    with pytest.raises(ValueError, match="can't start with '#'"):
        parse_config("companies:\n  - name: '#lease'\n    url: https://jobs.lever.co/x\n")


def test_json_backend_needs_no_aws(tmp_path):
    backend = open_backend(RuntimeSettings(state_file=str(tmp_path / "s.json")), "laptop:mac")
    assert isinstance(backend.store, JsonStateStore) and isinstance(backend.lease, LocalLease)


def test_dynamodb_backend_shares_one_table(table):
    from role_radar.dynamo import DynamoLease, DynamoStateStore

    runtime = RuntimeSettings(storage="dynamodb", table=table[1], region="us-east-1").with_env({})
    backend = open_backend(runtime, "lambda")
    assert isinstance(backend.store, DynamoStateStore) and isinstance(backend.lease, DynamoLease)
    assert backend.store.lease is backend.lease and backend.lease.acquire(60)


def test_config_is_read_from_s3_and_rereads_only_changes(fake_aws, monkeypatch):
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="role-radar-config")
    s3.put_object(Bucket="role-radar-config", Key="companies.yaml", Body=CONFIG.encode())
    runtime = RuntimeSettings(config_url="s3://role-radar-config/companies.yaml", region="us-east-1").with_env({})
    source = ConfigSource(runtime)

    bodies = []
    real_get = source._s3.s3.get_object
    monkeypatch.setattr(source._s3.s3, "get_object", lambda **kw: bodies.append("IfNoneMatch" in kw) or real_get(**kw))
    first = source.load()
    assert [c.name for c in first.companies] == ["Acme"] and first.settings.check_interval_minutes == 15
    assert source.load().companies[0].name == "Acme"  # served from the cached copy after a 304
    s3.put_object(Bucket="role-radar-config", Key="companies.yaml", Body=CONFIG.replace("Acme", "Globex").encode())
    assert source.load().companies[0].name == "Globex"
    assert bodies == [False, True, True]


def test_s3_urls_are_parsed_strictly():
    assert aws.parse_s3_url("s3://bucket/dir/companies.yaml") == ("bucket", "dir/companies.yaml")
    for bad in ("s3://bucket", "https://bucket/x", "s3:///x"):
        with pytest.raises(ValueError):
            aws.parse_s3_url(bad)


def test_secrets_come_from_ssm_once_and_only_when_needed(fake_aws, monkeypatch):
    import boto3

    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/hook", Type="SecureString")
    calls = []
    real = aws.ssm_parameters
    monkeypatch.setattr(aws, "ssm_parameters", lambda client, path: calls.append(path) or real(client, path))

    source = NotifierSource(RuntimeSettings(secrets="ssm:/role-radar/", region="us-east-1").with_env({}))
    assert calls == []  # nothing fetched until an alert needs sending
    (discord,) = source()
    assert isinstance(discord, DiscordNotifier)
    source()
    assert calls == ["/role-radar/"]


def test_secrets_from_the_environment():
    (console,) = NotifierSource(RuntimeSettings(), env={})()
    assert isinstance(console, ConsoleNotifier)
    (discord,) = NotifierSource(RuntimeSettings(), env={"DISCORD_WEBHOOK_URL": "https://discord.invalid/x"})()
    assert isinstance(discord, DiscordNotifier)


def test_secrets_are_reread_after_a_failed_delivery(fake_aws, monkeypatch):
    import boto3

    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/old", Type="SecureString")
    source = NotifierSource(RuntimeSettings(secrets="ssm:/role-radar/", region="us-east-1").with_env({}))
    (old,) = source()
    ssm.put_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/new", Type="SecureString", Overwrite=True)
    assert source()[0] is old  # cached
    source.invalidate()
    (new,) = source()
    assert new._url == "https://discord.invalid/new"


def test_ssm_without_channel_settings_gives_no_channels(fake_aws):
    source = NotifierSource(RuntimeSettings(secrets="ssm:/role-radar/", region="us-east-1").with_env({}))
    assert source() == []  # never a silent console fallback: alerts stay pending


def test_ssm_refresh_discovers_added_email_without_a_delivery_failure(fake_aws):
    import boto3
    from tests.conftest import Clock

    clock = Clock()
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/hook", Type="SecureString")
    source = NotifierSource(RuntimeSettings(secrets="ssm:/role-radar/", region="us-east-1"), clock=clock)
    assert [n.name for n in source()] == ["discord"]
    for key, value in {"SMTP_HOST": "smtp.invalid", "EMAIL_TO": "a@example.com"}.items():
        ssm.put_parameter(Name=f"/role-radar/{key}", Value=value, Type="SecureString")
    clock.advance(301)
    assert [n.name for n in source()] == ["discord", "email"]


def test_sending_stops_between_channels_once_the_lease_is_gone():
    import asyncio

    from role_radar.lease import LeaseLost
    from role_radar.notifications import notify_all
    from tests.conftest import job
    from tests.test_monitor import RecordingNotifier

    first, second = RecordingNotifier(), RecordingNotifier()
    checks = []

    def check():
        checks.append(1)
        if len(checks) > 1:  # the lid closed while the first channel was sending
            raise LeaseLost("lease expired")

    with pytest.raises(LeaseLost):
        asyncio.run(notify_all([first, second], [job()], check=check))
    assert len(first.batches) == 1 and second.batches == []
