"""First-run setup for the packaged app (`role-radar setup ...`, what its Setup window runs).

  init       create the companies file (settings, no companies yet) and a Mac-only profile
  show       what's set up: roles, companies, email, and whether it's ready to start
  profile    save what the person is looking for (roles, words to skip, places, experience)
  prompt     a prompt for ChatGPT or Claude that lists companies hiring for those roles
  companies  add companies from the AI's answer: known employers come from the
             directory (verified job boards), others are checked with one request first
  email      save a Gmail address and app password in the Keychain

The files it writes carry a GENERATED marker, and it refuses to rewrite files
without one, so a hand-edited companies.yaml or profile.yaml is never clobbered.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from role_radar.config import CompanyConfig, load_config, parse_raw, profile_path
from role_radar.filters import JobFilter
from role_radar.http_client import HttpClient
from role_radar.scrapers import scraper_class_for

GENERATED = "# Written by Role Radar's setup."
DEFAULT_EXCLUDE = ["senior", "sr", "staff", "principal", "lead", "director", "vice president", "vp", "head of", "chief"]
MAC_ONLY = {"storage": "sqlite", "secrets": "keychain"}
GMAIL = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls"}
CHECK_TIMEOUT = 45.0  # seconds for one company's check before it counts as not working

# Careers page formats the AI should give, by job board (what the scrapers read).
BOARD_FORMATS = [
    ("Greenhouse", "https://job-boards.greenhouse.io/COMPANY"),
    ("Lever", "https://jobs.lever.co/COMPANY"),
    ("Ashby", "https://jobs.ashbyhq.com/COMPANY"),
    ("Workday", "https://COMPANY.wd5.myworkdayjobs.com/SITE_NAME"),
    ("BambooHR", "https://COMPANY.bamboohr.com/careers"),
    ("Rippling", "https://ats.rippling.com/COMPANY/jobs"),
    ("iCIMS", "https://careers-COMPANY.icims.com/jobs"),
    ("Eightfold", "https://COMPANY.eightfold.ai/careers"),
    ("Oracle Cloud", "https://XXXX.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/SITE/requisitions"),
]


class NotGenerated(ValueError):
    """The file wasn't written by setup: rewriting it would lose someone's edits."""


# -- files ------------------------------------------------------------------------------


def template_text() -> str:
    """A new companies file: the check settings, and no companies yet."""
    return resources.files("role_radar").joinpath("templates/companies.yaml").read_text(encoding="utf-8")


def init(config: Path) -> None:
    """Create the companies file and the profile if they don't exist yet."""
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text(template_text(), encoding="utf-8")
    profile = profile_path(config)
    if not profile.exists():
        _write_profile(profile, MAC_ONLY, {"include_keywords": [], "exclude_keywords": DEFAULT_EXCLUDE,
                                           "match_on": ["title"], "exclude_on": ["title"]})


def _check_generated(path: Path) -> None:
    if path.exists() and not path.read_text(encoding="utf-8").startswith(GENERATED):
        raise NotGenerated(f"{path} wasn't written by setup; edit it by hand instead")


def _write_profile(path: Path, runtime: dict[str, Any], filters: dict[str, Any]) -> None:
    text = (f"{GENERATED} Change it in the app (Settings), or by hand.\n"
            "# runtime: where state and alert settings live; filters: what to look for.\n"
            + yaml.safe_dump({"runtime": runtime, "filters": filters}, sort_keys=False, allow_unicode=True))
    path.write_text(text, encoding="utf-8")


def _read_yaml(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


# -- show and profile ------------------------------------------------------------------------


def show(config: Path) -> dict[str, Any]:
    from role_radar import keychain

    filters = _read_yaml(profile_path(config)).get("filters") or {}
    companies = (_read_yaml(config).get("companies") or [])
    try:
        stored = keychain.read_all()
    except RuntimeError:  # not macOS
        stored = {}
    state = {
        "roles": filters.get("include_keywords") or [],
        "exclude": filters.get("exclude_keywords") or [],
        "locations": filters.get("locations") or [],
        "max_experience_years": filters.get("max_experience_years"),
        "companies": len(companies),
        "email": stored.get("EMAIL_TO"),
        "email_ready": bool(stored.get("EMAIL_TO") and stored.get("SMTP_PASSWORD")),
    }
    state["ready"] = bool(state["roles"] and state["companies"] and state["email_ready"])
    return state


def save_profile(config: Path, roles: list[str], exclude: list[str], locations: list[str],
                 max_experience_years: int | None) -> None:
    """Replace what the profile looks for; keep where state lives."""
    path = profile_path(config)
    _check_generated(path)
    clean = lambda words: [w.strip() for w in words if w and w.strip()]  # noqa: E731
    filters: dict[str, Any] = {"include_keywords": clean(roles), "exclude_keywords": clean(exclude),
                               "match_on": ["title"], "exclude_on": ["title"]}
    if clean(locations):
        filters["locations"] = clean(locations)
    if max_experience_years is not None:
        if not 0 <= int(max_experience_years) <= 30:
            raise ValueError("years of experience must be between 0 and 30")
        filters["max_experience_years"] = int(max_experience_years)
    if not filters["include_keywords"]:
        raise ValueError("add at least one role to look for")
    runtime = _read_yaml(path).get("runtime") or MAC_ONLY
    _write_profile(path, runtime, filters)
    load_config(config, path)  # never leave a profile that doesn't load


def save_email(address: str, app_password: str) -> None:
    """Gmail: send from and to the address, signing in with an app password (spaces don't matter)."""
    from role_radar import keychain

    address, password = address.strip(), re.sub(r"\s+", "", app_password)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise ValueError(f"{address!r} doesn't look like an email address")
    if not re.fullmatch(r"[A-Za-z]{16}", password):
        raise ValueError("a Gmail app password is 16 letters (Google shows it in groups of four)")
    for name, value in {**GMAIL, "EMAIL_TO": address, "EMAIL_FROM": address, "SMTP_USERNAME": address}.items():
        keychain.write(name, value)
    keychain.write("SMTP_PASSWORD", password)


# -- the prompt ------------------------------------------------------------------------


def prompt(config: Path) -> str:
    """What to paste into ChatGPT or Claude, built from the saved profile."""
    state = show(config)
    if not state["roles"]:
        raise ValueError("save the roles you're looking for first")
    years = state["max_experience_years"]
    level = ("entry-level roles (new graduates, no experience required)" if years == 0 else
             f"roles open to someone with up to {years} years of experience" if years is not None else "roles at any level")
    places = ", ".join(state["locations"]) if state["locations"] else "any location"
    formats = "\n".join(f"- {board}: {url}" for board, url in BOARD_FORMATS)
    return f"""I'm looking for {level} with these job titles: {", ".join(state["roles"])}.
Location: {places}.

List 100 companies that regularly hire for these roles in these locations. Mix large, well-known
employers with mid-size companies and startups.

For each company, give the URL of its careers job board. My tool can read these job boards
(capital letters are placeholders):
{formats}

Rules:
- Only include a URL you're confident is the company's real job board. If you're not sure,
  give just the name and leave out the URL: my tool looks up well-known companies itself.
- Don't include staffing agencies or job aggregator sites.

Reply with only this list, in this exact format, and nothing else:

- name: Company Name
  url: https://job-boards.greenhouse.io/companyname
- name: Another Company
"""


# -- companies ----------------------------------------------------------------------------


@dataclass
class Candidate:
    name: str
    url: str | None = None


@dataclass
class ImportReport:
    added: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"added": self.added, "skipped": self.skipped, "total": self.total}


def parse_candidates(text: str) -> list[Candidate]:
    """Companies from the AI's answer: the YAML list asked for, or failing that, one per line."""
    text = re.sub(r"^```[a-zA-Z]*\s*$", "", text, flags=re.MULTILINE)  # markdown code fences
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    found: list[Candidate] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                url = str(item.get("url") or "").strip() or None
                found.append(Candidate(str(item["name"]).strip(), url))
            elif isinstance(item, str) and item.strip():
                found.append(_from_line(item))
    else:
        found = [_from_line(line) for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    seen: set[str] = set()
    unique = []
    for candidate in found:
        if candidate.name and _key(candidate.name) not in seen:
            seen.add(_key(candidate.name))
            unique.append(candidate)
    return unique


def _from_line(line: str) -> Candidate:
    """"Stripe - https://..." or "Stripe" or "1. Stripe (https://...)"."""
    url = re.search(r"https?://\S+", line)
    name = re.sub(r"https?://\S+", "", line)
    name = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", name)
    name = re.sub(r"^name:\s*", "", name.strip(), flags=re.IGNORECASE)
    return Candidate(name.strip(" -–—:|()[]"), url.group(0).rstrip(").,") if url else None)


def _key(name: str) -> str:
    """A company name for matching: lowercase, no punctuation or legal suffix."""
    key = re.sub(r"[^a-z0-9 ]", "", name.casefold())
    key = re.sub(r"\s+(inc|llc|ltd|corp|corporation|co|company|plc|group|holdings)$", "", key.strip())
    return re.sub(r"\s+", " ", key).strip()


def load_directory(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Known employers with verified job boards (the full companies list), by name key.

    $ROLE_RADAR_DIRECTORY, else the package's copy. Their own filters are left behind.
    """
    path = Path(path or os.environ.get("ROLE_RADAR_DIRECTORY") or resources.files("role_radar").joinpath("templates/directory.yaml"))
    if not path.exists():
        return {}
    entries = _read_yaml(path).get("companies") or []
    return {_key(e["name"]): {k: v for k, v in e.items() if k != "filters"} for e in entries if e.get("name") and e.get("url")}


def import_companies(config: Path, text: str, *, replace: bool = False, check: bool = True,
                     directory: dict[str, dict[str, Any]] | None = None) -> ImportReport:
    """Add the companies in `text` to the companies file (or replace its list), and say what happened."""
    _check_generated(config)
    raw = _read_yaml(config)
    current = [] if replace else list(raw.get("companies") or [])
    have_names = {_key(c["name"]) for c in current}
    have_urls = {c["url"].rstrip("/").lower() for c in current}
    directory = load_directory() if directory is None else directory
    report = ImportReport()
    to_check: list[dict[str, Any]] = []
    for candidate in parse_candidates(text):
        known = directory.get(_key(candidate.name))
        entry = dict(known) if known else ({"name": candidate.name, "url": candidate.url} if candidate.url else None)
        if entry is None:
            report.skipped.append({"name": candidate.name, "reason": "no careers link given, and not a company it knows"})
        elif _key(entry["name"]) in have_names or entry["url"].rstrip("/").lower() in have_urls:
            report.skipped.append({"name": candidate.name, "reason": "already on your list"})
        else:
            have_names.add(_key(entry["name"]))
            have_urls.add(entry["url"].rstrip("/").lower())
            (current.append(entry) if known else to_check.append(entry))
            if known:
                report.added.append({"name": entry["name"], "source": "known"})
    if to_check:
        settings = parse_raw({**raw, "companies": []}).settings
        results = asyncio.run(_check_all(to_check, settings)) if check else [(e, None, None) for e in to_check]
        for entry, jobs, error in results:
            if error:
                report.skipped.append({"name": entry["name"], "reason": error})
            else:
                current.append(entry)
                report.added.append({"name": entry["name"], "source": "checked", "jobs": jobs})
    raw["companies"] = current
    parse_raw({**raw, "defaults": {"filters": {"include_keywords": ["x"]}}}, str(config))  # never write a broken file
    body = yaml.safe_dump({k: v for k, v in raw.items() if k != "companies"}, sort_keys=False, allow_unicode=True, width=120)
    config.write_text(f"{GENERATED} Add companies in the app (Settings), or by hand.\n{body}"
                      + yaml.safe_dump({"companies": current}, sort_keys=False, allow_unicode=True, width=120),
                      encoding="utf-8")
    report.total = len(current)
    return report


async def _check_all(entries: list[dict[str, Any]], settings) -> list[tuple[dict[str, Any], int | None, str | None]]:
    """Read each new company's job board once: does it work, and how many jobs does it list?"""
    gate = asyncio.Semaphore(6)
    async with HttpClient(settings.http) as http:
        async def one(entry: dict[str, Any]) -> tuple[dict[str, Any], int | None, str | None]:
            async with gate:
                cls = scraper_class_for(entry["url"], entry.get("ats"))
                if cls.name == "generic":
                    return entry, None, "not a job board it can read (ask for its Greenhouse, Lever, Workday... link)"
                company = CompanyConfig(name=entry["name"], url=entry["url"], filter=JobFilter(), ats=entry.get("ats"),
                                        options=entry.get("options") or {})
                try:
                    scraper = cls(company, http)
                    fetch = scraper.fetch_first_page() if scraper.supports_quick else scraper.fetch_jobs()
                    result = await asyncio.wait_for(fetch, timeout=CHECK_TIMEOUT)
                    jobs = len(result) if isinstance(result, list) else len(result.jobs)
                    return entry, jobs, None
                except Exception as exc:
                    return entry, None, f"its job board didn't answer ({type(exc).__name__})"
        return await asyncio.gather(*(one(e) for e in entries))
