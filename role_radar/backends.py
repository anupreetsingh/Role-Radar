"""Pick the backends from RuntimeSettings, so one codebase serves the laptop, Lambda and tests.

  storage: json       → JsonStateStore(state_file) + LocalLease (one process, nothing to share)
  storage: dynamodb   → DynamoStateStore + DynamoLease, sharing one table
  config_url: s3://…  → companies and settings come from that object; a local file
                        then only supplies the `runtime:` section
  secrets: ssm:/path/ → Discord/SMTP settings from Parameter Store, fetched the
                        first time an alert is actually sent
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
from role_radar.config import AppConfig, RuntimeSettings, load_runtime, parse_config
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
    """The local file's `runtime:` section (if the file exists) with environment overrides applied."""
    path = Path(local_config) if local_config else None
    base = load_runtime(path) if path and path.exists() else RuntimeSettings()
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

    def load(self) -> AppConfig:
        text = self.read_text()
        if self._s3 or not self.local_config:
            return parse_config(text, self.description)
        return parse_config(text, str(self.local_config), as_json=self.local_config.suffix == ".json")


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
                if path:
                    clients = self._clients or AwsClients(self.runtime)
                    settings = aws.ssm_parameters(clients.ssm, path)
                else:
                    settings = self._env
                # With SSM, no settings is a mistake: print nothing and keep alerts pending instead.
                self._notifiers = notifiers_from_env(settings, console_fallback=not path)
                self._loaded_at = self._clock()
                if self._notifiers:
                    log.info("Alert channels: %s", ", ".join(n.name for n in self._notifiers))
                else:
                    log.error("No alert channel settings under %s: add DISCORD_WEBHOOK_URL or SMTP_HOST + EMAIL_TO", path)
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
        return Backend(JsonStateStore(runtime.state_file), LocalLease(holder, clock))
    from role_radar.dynamo import DynamoLease, DynamoStateStore

    client = (clients or AwsClients(runtime)).dynamodb
    lease = DynamoLease(client, runtime.table, holder, clock)
    return Backend(DynamoStateStore(client, runtime.table, lease, clock), lease)
