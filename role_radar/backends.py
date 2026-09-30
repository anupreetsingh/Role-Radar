"""Pick the backends from RuntimeSettings, so one codebase serves the laptop, Lambda and tests.

  storage: sqlite     → SqliteStateStore(state_file) + LocalLease: everything on this Mac
  storage: dynamodb   → DynamoStateStore + DynamoLease, sharing one table with Lambda
  storage: json       → JsonStateStore(state_file) + LocalLease (one process: tests, dry runs)
  config_url: s3://…  → companies and settings come from that object (what `config push`
                        uploaded); the local files then only supply the `runtime:` section
  secrets: keychain   → Discord/SMTP settings from this Mac's Keychain (keychain.py)
  secrets: ssm:/path/ → Discord/SMTP settings from Parameter Store, fetched the
                        first time an alert is actually sent

The local config is the companies file with the profile beside it applied
(config.py): the profile holds the `runtime:` section and the filters.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Mapping

from role_radar import aws
from role_radar.config import AppConfig, RuntimeSettings, load_config, load_runtime, parse_config, profile_path
from role_radar.lease import Lease, LocalLease
from role_radar.notifications import Notifier, notifiers_from_env
from role_radar.storage import JsonStateStore, StateStore

log = logging.getLogger(__name__)


class AwsClients:
    """boto3 clients for one runtime, created on first use."""

    def __init__(self, runtime: RuntimeSettings) -> None:
        self.runtime = runtime

    @cached_property
    def session(self) -> Any:
        return aws.session(self.runtime.region, self.runtime.profile)

    @cached_property
    def dynamodb(self) -> Any:
        return aws.client(self.session, "dynamodb")

    @cached_property
    def s3(self) -> Any:
        return aws.client(self.session, "s3")

    @cached_property
    def ssm(self) -> Any:
        return aws.client(self.session, "ssm")


def resolve_runtime(local_config: str | Path | None, env: Mapping[str, str] | None = None) -> RuntimeSettings:
    """The `runtime:` section of the local config (its profile's, if it has one) with environment overrides applied."""
    path = Path(local_config) if local_config else None
    base = load_runtime(path, profile_path(path)) if path and path.exists() else RuntimeSettings()
    return base.with_env(env)


class ConfigSource:
    """The companies config: the local file, or the copy pushed to runtime.config_url.

    Both the laptop and Lambda read the S3 copy, so they always agree on what
    to check. Re-reading S3 is cheap: after the first read, S3 only sends the
    object again when it has changed.
    """

    def __init__(self, runtime: RuntimeSettings, local_config: str | Path | None = None, clients: AwsClients | None = None) -> None:
        self.runtime = runtime
        self.local_config = Path(local_config) if local_config else None
        self._s3 = aws.S3Text((clients or AwsClients(runtime)).s3, runtime.config_url) if runtime.config_url else None

    @property
    def description(self) -> str:
        return self.runtime.config_url or str(self.local_config)

    def read_text(self) -> str:
        if self._s3:
            return self._s3.read()
        if not self.local_config:
            raise FileNotFoundError(
                "no config: pass --config, set ROLE_RADAR_CONFIG_FILE, or set runtime.config_url / ROLE_RADAR_CONFIG_URL"
            )
        return self.local_config.read_text(encoding="utf-8")

    def stamp(self) -> tuple[int, ...] | None:
        """When the local files last changed (None for the S3 copy), so a change saved in Setup is read at once."""
        if self._s3 or not self.local_config:
            return None
        files = (self.local_config, profile_path(self.local_config))
        return tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)

    def load(self) -> AppConfig:
        if self._s3 or not self.local_config:
            config = parse_config(self.read_text(), self.description)
        else:
            config = load_config(self.local_config, profile_path(self.local_config))
        require_filters(config, self.description)
        return config


def require_filters(config: AppConfig, source: str) -> None:
    """Refuse a config with no roles to look for: every job would match, and each would be alerted."""
    if config.companies and not any(c.filter.include_keywords for c in config.companies):
        raise ValueError(
            f"{source}: no roles to look for. Put include_keywords under `filters:` in your profile "
            "(config/profile.yaml; start from config/profile.example.yaml)"
        )


class NotifierSource:
    """The alert channels, built the first time they're needed.

    With secrets in SSM that means Parameter Store is only called when there's
    actually something to send, then at most every five minutes.
    """

    def __init__(
        self, runtime: RuntimeSettings, clients: AwsClients | None = None, env: Mapping[str, str] | None = None,
        *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self._clients = clients
        self._env = env
        self._notifiers: list[Notifier] | None = None
        self._lock = threading.Lock()
        self._clock = clock
        self._loaded_at = 0.0

    def __call__(self) -> list[Notifier]:
        with self._lock:
            if self._notifiers is None or self._clock() - self._loaded_at >= 300:
                path = self.runtime.ssm_path
                stored = bool(path) or self.runtime.secrets == "keychain"
                if path:
                    clients = self._clients or AwsClients(self.runtime)
                    settings = aws.ssm_parameters(clients.ssm, path)
                elif self.runtime.secrets == "keychain":
                    from role_radar import keychain

                    settings = keychain.read_all()
                else:
                    settings = self._env
                # With stored settings, none is a mistake: print nothing and keep alerts pending instead.
                self._notifiers = notifiers_from_env(settings, console_fallback=not stored)
                self._loaded_at = self._clock()
                if self._notifiers:
                    log.info("Alert channels: %s", ", ".join(n.name for n in self._notifiers))
                else:
                    log.error("No alert channel settings in %s: add DISCORD_WEBHOOK_URL or SMTP_HOST + EMAIL_TO",
                              path or "the Keychain (role-radar secrets set NAME)")
            return self._notifiers

    def invalidate(self) -> None:
        """Forget the channels, so the next alert re-reads the settings (e.g. after a rotated webhook)."""
        with self._lock:
            self._notifiers = None


@dataclass
class Backend:
    store: StateStore
    lease: Lease


def open_backend(
    runtime: RuntimeSettings, holder: str, clients: AwsClients | None = None, clock: Callable[[], float] = time.time
) -> Backend:
    """The state store and the lease for `holder` (e.g. "laptop:<host>", "lambda")."""
    if runtime.storage == "json":
        return Backend(JsonStateStore(runtime.state_path), LocalLease(holder, clock))
    if runtime.storage == "sqlite":
        from role_radar.sqlite import SqliteStateStore

        return Backend(SqliteStateStore(runtime.state_path, clock), LocalLease(holder, clock))
    from role_radar.dynamo import DynamoLease, DynamoStateStore

    client = (clients or AwsClients(runtime)).dynamodb
    lease = DynamoLease(client, runtime.table, holder, clock)
    return Backend(DynamoStateStore(client, runtime.table, lease, clock), lease)
