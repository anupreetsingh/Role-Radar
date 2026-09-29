"""Load and validate the companies file (YAML or JSON), with the profile next to it.

The companies file has four sections: `settings` (how checks run), `defaults`
(filters every company inherits), `companies`, and `runtime` (where state,
config and secrets live: see RuntimeSettings).

The profile (profile.yaml beside the companies file, or $ROLE_RADAR_PROFILE)
holds one person's own settings, kept out of git: `runtime`, `filters` (the
roles, places and experience they want: defaults.filters, key by key) and
optionally `settings` overrides. Applied over the companies file, its
sections win. Without a profile, the companies file alone is the config.
"""

from __future__ import annotations

import json
import math
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
    # Don't alert on a new match posted more than this many days before it was first
    # seen: an old job surfacing late (e.g. while a sitemap is being worked through).
    max_alert_age_days: int | None = None


@dataclass
class Settings:
    # How often each company is checked. Companies are checked when due, not all at once.
    check_interval_minutes: float = 30.0
    # 0: immediate company batches; otherwise combine pending jobs on this cadence.
    digest_interval_minutes: float = 0.0
    # Alert on matches already open the first time a company is checked.
    # Set false to silently record a baseline when adding many companies at once.
    notify_on_first_run: bool = True
    # Removed jobs are forgotten after this many days to keep the state small.
    retention_days: int = 90
    # Cap on per-job detail requests per company per run (remaining ones are fetched next run).
    max_detail_requests: int = 25
    # Companies checked at once. Requests are still limited by http.max_concurrency
    # and each host's delay; this just keeps enough work queued for other hosts.
    max_company_concurrency: int = 40
    company_timeout: float = 300.0
    # Per-ATS overrides of check_interval_minutes, e.g. {"workday": 180}, for platforms
    # whose boards take many requests each.
    check_interval_by_ats: dict[str, float] = field(default_factory=dict)
    # Per-ATS caps on companies checked at once, e.g. {"workday": 2}. A company waits
    # for its ATS's turn before taking one of max_company_concurrency's slots.
    company_concurrency_by_ats: dict[str, int] = field(default_factory=dict)
    # Per-ATS quick checks between full checks, e.g. {"workday": 10}: every N minutes read
    # only the newest page, and read further only if it shows jobs it didn't show last time.
    # Only for platforms that list newest first (the scraper must support it).
    quick_check_by_ats: dict[str, float] = field(default_factory=dict)
    http: HttpSettings = field(default_factory=HttpSettings)

    def __post_init__(self) -> None:
        if not math.isfinite(self.digest_interval_minutes) or self.digest_interval_minutes < 0:
            raise ValueError("settings.digest_interval_minutes must be a finite nonnegative number")
        self.check_interval_by_ats = _minutes_by_ats(self.check_interval_by_ats, "check_interval_by_ats")
        self.quick_check_by_ats = _minutes_by_ats(self.quick_check_by_ats, "quick_check_by_ats")
        limits = {}
        for ats, limit in (self.company_concurrency_by_ats or {}).items():
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError(f"settings.company_concurrency_by_ats[{ats!r}] must be a whole number >= 1")
            limits[str(ats).strip().lower()] = limit
        self.company_concurrency_by_ats = limits

    @property
    def check_interval(self) -> timedelta:
        return timedelta(minutes=self.check_interval_minutes)

    def check_interval_for(self, ats: str | None) -> timedelta:
        """The check interval for companies on `ats` (a scraper name)."""
        minutes = self.check_interval_by_ats.get((ats or "").lower())
        return timedelta(minutes=minutes) if minutes else self.check_interval

    def quick_interval_for(self, ats: str | None) -> timedelta | None:
        """How often companies on `ats` get a quick check, or None if they don't."""
        minutes = self.quick_check_by_ats.get((ats or "").lower())
        return timedelta(minutes=minutes) if minutes else None


def _minutes_by_ats(values: Mapping[str, float] | None, name: str) -> dict[str, float]:
    minutes_by_ats = {}
    for ats, minutes in (values or {}).items():
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or not math.isfinite(minutes) or minutes <= 0:
            raise ValueError(f"settings.{name}[{ats!r}] must be a positive number of minutes")
        minutes_by_ats[str(ats).strip().lower()] = float(minutes)
    return minutes_by_ats


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

    # sqlite: everything on this Mac (state_file, default ~/.role-radar/state.db);
    # dynamodb: the AWS table, shared with Lambda; json: one file (tests, dry runs).
    storage: str = "json"
    state_file: str | None = None
    table: str | None = None
    # Where the companies config is read from: s3://bucket/key. Unset: the local file itself.
    config_url: str | None = None
    # env (environment variables) | keychain (this Mac's Keychain) | ssm:/path/ (Parameter Store)
    secrets: str = "env"
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
        if self.storage not in ("json", "sqlite", "dynamodb"):
            raise ValueError(f"runtime.storage must be 'sqlite', 'dynamodb' or 'json', not {self.storage!r}")
        if self.storage == "dynamodb" and not self.table:
            raise ValueError("runtime.storage is dynamodb but no table is set (runtime.table or ROLE_RADAR_TABLE)")
        if self.secrets not in ("env", "keychain") and not (self.secrets.startswith("ssm:/") and self.secrets.endswith("/")):
            raise ValueError(f"runtime.secrets must be 'env', 'keychain' or 'ssm:/path/', not {self.secrets!r}")
        if self.config_url and not self.config_url.startswith("s3://"):
            raise ValueError(f"runtime.config_url must be an s3:// URL, not {self.config_url!r}")

    @property
    def ssm_path(self) -> str | None:
        return self.secrets[len("ssm:") :] if self.secrets.startswith("ssm:") else None

    @property
    def state_path(self) -> Path:
        """The local state file: state_file, else the default for the storage."""
        if self.state_file:
            return Path(self.state_file).expanduser()
        if self.storage == "sqlite":
            from role_radar.instance import home

            return home() / "state.db"
        return Path("seen_jobs.json")


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


# libyaml's parser when PyYAML was built with it: ten times faster on a file of thousands of companies.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


PROFILE_FILE = "profile.yaml"
PROFILE_SECTIONS = ("runtime", "filters", "settings")


def profile_path(config_path: str | Path) -> Path:
    """Where the profile for a companies file lives: $ROLE_RADAR_PROFILE, else profile.yaml beside it."""
    if os.environ.get("ROLE_RADAR_PROFILE"):
        return Path(os.environ["ROLE_RADAR_PROFILE"]).expanduser()
    return Path(config_path).with_name(PROFILE_FILE)


def load_config(path: str | Path, profile: str | Path | None = None) -> AppConfig:
    """The companies file at `path`, with `profile` applied if given and present."""
    return parse_raw(combined(path, profile), str(path))


def load_runtime(path: str | Path, profile: str | Path | None = None) -> RuntimeSettings:
    """Only the `runtime:` section, for commands that just need to know where state lives.

    Skips building every company's filter, which takes seconds for thousands of companies.
    """
    raw = combined(path, profile)
    return _build(RuntimeSettings, dict(raw.get("runtime") or {}), "runtime")


def combined(path: str | Path, profile: str | Path | None = None) -> dict[str, Any]:
    """The companies file's contents with the profile's sections applied (what `config push` uploads)."""
    path = Path(path)
    raw = _read_raw(path.read_text(encoding="utf-8"), str(path), as_json=path.suffix == ".json")
    return apply_profile(raw, read_profile(profile)) if profile else raw


def read_profile(path: str | Path) -> dict[str, Any] | None:
    """A profile's sections, or None if there's no such file."""
    path = Path(path)
    if not path.exists():
        return None
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping with {', '.join(PROFILE_SECTIONS)}")
    unknown = set(raw) - set(PROFILE_SECTIONS)
    if unknown:
        raise ValueError(f"{path}: unknown sections {sorted(unknown)}; a profile has {', '.join(PROFILE_SECTIONS)}")
    for name in PROFILE_SECTIONS:
        if raw.get(name) is not None and not isinstance(raw[name], dict):
            raise ValueError(f"{path}: `{name}` must be a mapping")
    return raw


def apply_profile(raw: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
    """The companies file's contents with a profile applied.

    Its runtime replaces the file's; its filters and settings replace the file's
    defaults.filters and settings key by key (settings.http too).
    """
    if not profile:
        return raw
    out = dict(raw)
    if profile.get("runtime") is not None:
        out["runtime"] = dict(profile["runtime"])
    if profile.get("filters"):
        defaults = dict(out.get("defaults") or {})
        defaults["filters"] = {**(defaults.get("filters") or {}), **profile["filters"]}
        out["defaults"] = defaults
    if profile.get("settings"):
        settings = dict(out.get("settings") or {})
        for key, value in profile["settings"].items():
            both_maps = isinstance(value, dict) and isinstance(settings.get(key), dict)
            settings[key] = {**settings[key], **value} if both_maps else value
        out["settings"] = settings
    return out


def split_profile(raw: dict[str, Any]) -> dict[str, Any]:
    """The profile part of combined contents (`config pull` restores a lost profile from the pushed copy)."""
    profile: dict[str, Any] = {}
    if raw.get("runtime"):
        profile["runtime"] = raw["runtime"]
    if (raw.get("defaults") or {}).get("filters"):
        profile["filters"] = raw["defaults"]["filters"]
    return profile


def _read_raw(text: str, source: str, *, as_json: bool) -> dict[str, Any]:
    raw = json.loads(text) if as_json else yaml.load(text, Loader=_YAML_LOADER)
    if not isinstance(raw, dict) or not isinstance(raw.get("companies"), list):
        raise ValueError(f"{source}: expected a mapping with a 'companies' list")
    return raw


def parse_config(text: str, source: str = "config", *, as_json: bool = False) -> AppConfig:
    """Parse and validate a companies file's contents. `source` names it in errors."""
    return parse_raw(_read_raw(text, source, as_json=as_json), source)


def parse_raw(raw: dict[str, Any], source: str = "config") -> AppConfig:
    """Validate a companies file's parsed contents (a profile already applied)."""
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
