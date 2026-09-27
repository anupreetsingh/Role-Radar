"""Load and validate the companies file (YAML or JSON)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from filters import JobFilter
from http_client import HttpSettings


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
    state_file: str = "seen_jobs.json"
    # Alert on matches already open the first time a company is checked.
    # Set false to silently record a baseline when adding many companies at once.
    notify_on_first_run: bool = True
    # A job reposted under a new ID within this many days of removal is not re-alerted.
    repost_window_days: int = 30
    # Removed jobs are forgotten after this many days to keep the state file small.
    retention_days: int = 90
    # Cap on per-job detail requests per company per run (remaining ones are fetched next run).
    max_detail_requests: int = 25
    # Companies checked at once. Requests are still limited by http.max_concurrency
    # and each host's delay; this just keeps enough work queued for other hosts.
    max_company_concurrency: int = 40
    company_timeout: float = 300.0
    http: HttpSettings = field(default_factory=HttpSettings)


@dataclass
class AppConfig:
    settings: Settings
    companies: list[CompanyConfig]


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("companies"), list):
        raise ValueError(f"{path}: expected a mapping with a 'companies' list")

    settings_raw = dict(raw.get("settings") or {})
    http = _build(HttpSettings, settings_raw.pop("http", None) or {}, "settings.http")
    settings = _build(Settings, {**settings_raw, "http": http}, "settings")

    default_filters = (raw.get("defaults") or {}).get("filters") or {}
    companies: list[CompanyConfig] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw["companies"]):
        where = f"companies[{i}]"
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            raise ValueError(f"{where}: 'name' and 'url' are required")
        entry = dict(entry)
        if entry["name"] in seen_names:
            raise ValueError(f"{where}: duplicate company name {entry['name']!r}")
        seen_names.add(entry["name"])
        # Company filter keys replace (not extend) the defaults, key by key.
        filters = {**default_filters, **(entry.pop("filters", None) or {})}
        try:
            job_filter = JobFilter.from_config(filters)
        except ValueError as exc:
            raise ValueError(f"{where} ({entry['name']}): {exc}") from exc
        entry["options"] = entry.get("options") or {}
        companies.append(_build(CompanyConfig, {**entry, "filter": job_filter}, where))
    return AppConfig(settings=settings, companies=companies)
