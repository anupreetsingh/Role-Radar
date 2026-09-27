"""Load and validate the companies file (YAML or JSON).

The file has four sections: `settings` (how checks run), `defaults` (filters
every company inherits), `companies`, and `runtime` (where state, config and
secrets live: see RuntimeSettings).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from role_radar.filters import JobFilter
from role_radar.http_client import HttpSettings

# Rows the state store keeps for itself use keys starting with this.
RESERVED_PREFIX = "#"


@dataclass
class CompanyConfig:
    name: str
    url: str
    filter: JobFilter
    ats: str | None = None  # None → auto-detect from the URL
    options: dict[str, Any] = field(default_factory=dict)  # scraper-specific settings
    enabled: bool = True


@dataclass
class Settings:
    # How often each company is checked. Companies are checked when due, not all at once.
    check_interval_minutes: float = 30.0
    # Alert on matches already open the first time a company is checked.
    # Set false to silently record a baseline when adding many companies at once.
    notify_on_first_run: bool = True
    # A job reposted under a new ID within this many days of removal is not re-alerted.
    repost_window_days: int = 30
    # Removed jobs are forgotten after this many days to keep the state small.
    retention_days: int = 90
    # Cap on per-job detail requests per company per run (remaining ones are fetched next run).
    max_detail_requests: int = 25
    # Companies checked at once. Requests are still limited by http.max_concurrency
    # and each host's delay; this just keeps enough work queued for other hosts.
    max_company_concurrency: int = 40
    company_timeout: float = 300.0
    http: HttpSettings = field(default_factory=HttpSettings)

    @property
    def check_interval(self) -> timedelta:
        return timedelta(minutes=self.check_interval_minutes)


# RuntimeSettings field → environment variables that override it, first one set wins.
RUNTIME_ENV: dict[str, tuple[str, ...]] = {
    "storage": ("ROLE_RADAR_STORAGE",),
    "state_file": ("ROLE_RADAR_STATE_FILE",),
    "table": ("ROLE_RADAR_TABLE",),
    "config_url": ("ROLE_RADAR_CONFIG_URL",),
    "secrets": ("ROLE_RADAR_SECRETS",),
    "region": ("ROLE_RADAR_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
    "profile": ("ROLE_RADAR_PROFILE", "AWS_PROFILE"),
}


@dataclass
class RuntimeSettings:
    """Which backends to use, so one codebase serves the laptop, Lambda and tests.

    Each value comes from the first of: an environment variable (RUNTIME_ENV),
    the `runtime:` section of the local config file, the default below.
    """

    storage: str = "json"  # json (state_file) | dynamodb (table)
    state_file: str = "seen_jobs.json"
    table: str | None = None
    # Where the companies config is read from: s3://bucket/key. Unset: the local file itself.
    config_url: str | None = None
    secrets: str = "env"  # env (environment variables) | ssm:/path/ (Parameter Store)
    region: str | None = None
    profile: str | None = None

    def with_env(self, env: Mapping[str, str] | None = None) -> RuntimeSettings:
        """These settings with environment overrides applied, validated."""
        env = os.environ if env is None else env
        overrides = {}
        for name, keys in RUNTIME_ENV.items():
            value = next((env[k] for k in keys if env.get(k)), None)
            if value:
                overrides[name] = value
        resolved = replace(self, **overrides)
        resolved.validate()
        return resolved

    def validate(self) -> None:
        if self.storage not in ("json", "dynamodb"):
            raise ValueError(f"runtime.storage must be 'json' or 'dynamodb', not {self.storage!r}")
        if self.storage == "dynamodb" and not self.table:
            raise ValueError("runtime.storage is dynamodb but no table is set (runtime.table or ROLE_RADAR_TABLE)")
        if self.secrets != "env" and not (self.secrets.startswith("ssm:/") and self.secrets.endswith("/")):
            raise ValueError(f"runtime.secrets must be 'env' or 'ssm:/path/', not {self.secrets!r}")
        if self.config_url and not self.config_url.startswith("s3://"):
            raise ValueError(f"runtime.config_url must be an s3:// URL, not {self.config_url!r}")

    @property
    def ssm_path(self) -> str | None:
        return self.secrets[len("ssm:") :] if self.secrets.startswith("ssm:") else None


@dataclass
class AppConfig:
    settings: Settings
    companies: list[CompanyConfig]
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)  # before environment overrides


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    return parse_config(path.read_text(encoding="utf-8"), str(path), as_json=path.suffix == ".json")


def parse_config(text: str, source: str = "config", *, as_json: bool = False) -> AppConfig:
    """Parse and validate a companies file's contents. `source` names it in errors."""
    raw = json.loads(text) if as_json else yaml.safe_load(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("companies"), list):
        raise ValueError(f"{source}: expected a mapping with a 'companies' list")

    settings_raw = dict(raw.get("settings") or {})
    http = _build(HttpSettings, settings_raw.pop("http", None) or {}, "settings.http")
    settings = _build(Settings, {**settings_raw, "http": http}, "settings")
    runtime = _build(RuntimeSettings, dict(raw.get("runtime") or {}), "runtime")

    default_filters = (raw.get("defaults") or {}).get("filters") or {}
    companies: list[CompanyConfig] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw["companies"]):
        company = _parse_company(entry, f"companies[{i}]", default_filters)
        if company.name in seen_names:
            raise ValueError(f"companies[{i}]: duplicate company name {company.name!r}")
        seen_names.add(company.name)
        companies.append(company)
    return AppConfig(settings=settings, companies=companies, runtime=runtime)


def _parse_company(entry: Any, where: str, default_filters: dict[str, Any]) -> CompanyConfig:
    if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
        raise ValueError(f"{where}: 'name' and 'url' are required")
    entry = dict(entry)
    if str(entry["name"]).startswith(RESERVED_PREFIX):
        raise ValueError(f"{where}: company names can't start with {RESERVED_PREFIX!r}")
    # Company filter keys replace (not extend) the defaults, key by key.
    filters = {**default_filters, **(entry.pop("filters", None) or {})}
    try:
        job_filter = JobFilter.from_config(filters)
    except ValueError as exc:
        raise ValueError(f"{where} ({entry['name']}): {exc}") from exc
    entry["options"] = entry.get("options") or {}
    return _build(CompanyConfig, {**entry, "filter": job_filter}, where)
