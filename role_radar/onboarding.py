"""First-run setup for the packaged app (`role-radar setup ...`, what its Setup window runs).

  init       create the companies file (settings, no companies yet) and a Mac-only profile
  show       what's set up: profession, roles, countries, companies, email, and whether it's ready
  profession pick Tech, Accounting & Finance or Healthcare: its company list comes with the app
             (config.profession_list), and a new profession brings its titles and rule-out words
  profile    save what the person is looking for (target and non-target roles, countries and cities,
             experience and education)
  prompt     a prompt for ChatGPT or Claude that lists companies hiring for those roles
  companies  add companies of their own, e.g. from the AI's answer: known employers come from the
             professions' lists (verified job boards), others are checked with one request first
  email      save a Gmail address and app password in the Keychain
  discord    save a Discord channel's webhook in the Keychain (alerts are optional: email, Discord, both or neither)
  recipients who else gets the alerts (friends, a school address), besides the Gmail they come from

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

from role_radar.config import PROFESSIONS as PROFESSION_IDS
from role_radar.config import CompanyConfig, combined, load_config, parse_raw, profession_list, profile_path
from role_radar.filters import EDUCATION, JobFilter
from role_radar.http_client import HttpClient
from role_radar.scrapers import scraper_class_for

GENERATED = "# Written by Role Radar's setup."
DEFAULT_EXCLUDE = ["senior", "sr", "staff", "principal", "lead", "director", "vice president", "vp", "head of", "chief"]

# The professions Setup offers (config.PROFESSIONS). Each has its own company list, and two sets of
# boxes in groups, all ticked to start with: job titles that alert ("target roles"), and words that
# rule a title out ("non-target roles"), set for someone early in their career. A rule written as a
# pattern shows under a label: (label, pattern). Tech's are the titles tuned for software roles.
PROFESSIONS: dict[str, dict[str, Any]] = {
    "tech": {
        "name": "Tech",
        "about": "Software, data, AI, cloud and IT",
        "groups": [
            ("Software development", ["software engineer", "software engineering", "software development", "developer",
                                      "programmer", "SWE", "SDE", "full stack", "front end", "back end", "web engineer",
                                      "UI engineer", "Python engineer", "product engineer", "application engineer",
                                      "integration engineer", "integrations engineer"]),
            ("Cloud, platforms and DevOps", ["platform engineer", "platforms engineer", "platform engineering", "cloud engineer",
                                             "infrastructure engineer", "site reliability", "SRE", "DevOps", "DevSecOps",
                                             "production engineer", "build engineer", "release engineer", "tools engineer"]),
            ("Systems, performance and graphics", ["distributed systems", "systems development", "compiler",
                                                   "toolchain engineer", "runtime engineer", "kernel engineer",
                                                   "performance engineer", "GPU engineer", "graphics engineer",
                                                   "rendering engineer", "computer graphics", "gameplay engineer",
                                                   "algorithm engineer", "algorithms engineer", "high performance computing",
                                                   "HPC engineer", "member of technical staff", "member technical staff",
                                                   "associate technical staff"]),
            ("AI and machine learning", ["machine learning", "ML engineer", "MLOps", "LLMOps", "AI engineer",
                                         "AI engineering", "applied AI", "artificial intelligence", "generative AI",
                                         "gen AI engineer", "LLM engineer", "agent engineer", "agents engineer", "AI agent",
                                         "AI agents", "conversational AI", "deep learning", "NLP engineer",
                                         "natural language processing", "computer vision", "perception engineer",
                                         "inference engineer", "search engineer", "information retrieval",
                                         "research engineer", "applied scientist", "AI residency"]),
            ("Data", ["data engineer", "data engineering", "data scientist", "data science", "analytics engineer",
                      "database engineer", "ETL engineer"]),
            ("Testing and compliance", ["SDET", "QA engineer", "quality assurance engineer", "QA automation",
                                        "test automation", "software test", "GRC engineer"]),
            ("Product and program management", ["technical program manager", "technical program management",
                                                "associate product manager", "product manager"]),
            ("Solutions and deployment", ["forward deployed engineer", "forward deployment engineer",
                                          "AI deployment engineer", "solutions engineer", "solution engineer",
                                          "solutions architect", "solution architect"]),
            ("Graduate programs", ["engineering development group", "EDG", "technology development program",
                                   "technology analyst program", "technical development program",
                                   "software development program", "technology graduate"]),
        ],
        # Staff and manager spare Member of Technical Staff, and technical program and product managers.
        "skip": [
            ("Senior levels", ["senior", "sr",
                               ("staff", r"re:^(?!.*\b(?:member|associate)\b.*\btechnical[\s/_-]+staff\b).*\bstaff\b"),
                               "principal", "distinguished", "lead",
                               ("manager", r"re:^(?!.*\b(?:technical[\s/_-]+program|product)[\s/_-]+manager\b).*\bmanagers?\b")]),
            ("Executives", ["director", "vice president", "vp", "head of", "chief", "president", "officer"]),
            ("Other kinds of work", [
                ("SAP and ServiceNow", r"re:\b(?:ServiceNow|SAP|ABAP|Pega|MuleSoft|PeopleSoft|Fiori|Power[\s-]*Platform)\b"),
                ("Salesforce", r"re:\bSalesforce[\s/_-]+(?:developer|engineer|architect|administrator|consultant)\b"),
                ("content developer",
                 r"re:\b(?:courseware|content|documentation|information|course|curriculum|certification)[\s/_-]+developer\b"),
                ("firmware and hardware", r"re:\b(?:firmware|BIOS|UEFI|PCB|FPGA|ASIC|RTL|CNC|CMM|PLC|HVAC)\b"),
                "design release", "process integration", "supplier", "technician"]),
        ],
    },
    "accounting": {
        "name": "Accounting & Finance",
        "about": "Accounting, audit, tax, FP&A and payroll",
        "groups": [
            ("Accounting", ["accountant", "staff accountant", "junior accountant", "associate accountant",
                            "accounting associate", "accounting analyst", "general ledger", "GL accountant",
                            "cost accountant", "revenue accountant", "project accountant", "fixed asset accountant",
                            "property accountant", "management accountant", "graduate accountant", "reconciliation",
                            "bookkeeper"]),
            ("Payables, receivables and payroll", ["accounts payable", "accounts receivable", "AP specialist",
                                                   "AR specialist", "billing specialist", "credit controller", "payroll",
                                                   "payroll accountant", "payroll specialist"]),
            ("Audit and assurance", ["auditor", "audit associate", "audit assistant", "audit staff", "internal audit",
                                     "external audit", "statutory audit", "audit & assurance", "assurance associate",
                                     "SOX", "articleship", "article assistant"]),
            ("Tax", ["tax associate", "tax accountant", "tax analyst", "tax consultant", "tax preparer", "tax intern",
                     "indirect tax", "GST", "transfer pricing"]),
            ("Financial planning and analysis", ["financial analyst", "finance analyst", "FP&A", "financial planning",
                                                 "budget analyst", "business finance", "commercial finance",
                                                 "strategic finance", "finance associate"]),
            ("Reporting and controllership", ["financial reporting", "SEC reporting", "consolidation",
                                              "technical accounting", "financial controller", "assistant controller"]),
            ("Treasury, credit and funds", ["treasury analyst", "credit analyst", "fund accountant", "fund accounting",
                                            "fund administration", "investment accountant", "valuation analyst"]),
            ("Qualifications and programs", ["chartered accountant", "CA fresher", "semi qualified", "ACCA",
                                             "finance graduate", "accounting intern", "finance intern"]),
        ],
        "skip": [  # not "staff": Staff Accountant
            ("Senior levels", ["senior", "sr", "principal", "lead", "manager", "mgr", "supervisor"]),
            ("Executives", ["director", "vice president", "vp", "head of", "chief", "partner"]),
            ("Other kinds of work", ["sales", "account executive", "account manager", "recruiter", "software", "engineer",
                                     "developer", "SAP", "Oracle", "Workday", "NetSuite", "customer", "teller",
                                     "loan officer", "insurance agent", "quality assurance", "QA", "cyber", "security"]),
        ],
    },
    "healthcare": {
        "name": "Healthcare",
        "about": "Doctors, nurses, pharmacists, therapists and technologists",
        "groups": [
            ("Nursing", ["registered nurse", "RN", "staff nurse", "graduate nurse", "new grad RN", "nurse resident",
                         "enrolled nurse", "LPN", "LVN", "RPN", "clinical nurse", "nurse practitioner", "NP", "midwife",
                         "nursing assistant", "nurse"]),
            ("Doctors", ["physician", "doctor", "medical officer", "resident medical officer", "RMO", "junior resident",
                         "resident physician", "house officer", "medical intern", "registrar", "hospitalist",
                         "general practitioner", "GP", "family medicine", "internal medicine", "emergency medicine",
                         "MBBS"]),
            ("Specialists", ["surgeon", "anesthesiologist", "anaesthetist", "psychiatrist", "pediatrician",
                             "paediatrician", "radiologist", "cardiologist", "oncologist", "neurologist", "obstetrician",
                             "gynecologist", "dermatologist"]),
            ("Advanced practice", ["physician assistant", "PA-C", "CRNA", "nurse anesthetist"]),
            ("Pharmacy", ["pharmacist", "clinical pharmacist", "pharmacy resident"]),
            ("Therapy and rehabilitation", ["physical therapist", "physiotherapist", "occupational therapist",
                                            "speech language pathologist", "speech therapist", "respiratory therapist",
                                            "exercise physiologist"]),
            ("Imaging and laboratory", ["radiologic technologist", "radiographer", "sonographer", "ultrasound technologist",
                                        "MRI technologist", "CT technologist", "medical laboratory scientist",
                                        "medical technologist"]),
            ("Dental, eye and hearing", ["dentist", "dental hygienist", "dental therapist", "optometrist", "audiologist"]),
            ("Mental health", ["psychologist", "clinical psychologist", "psychotherapist", "mental health counselor",
                               "counsellor"]),
            ("Emergency and nutrition", ["paramedic", "EMT", "dietitian", "dietician", "nutritionist"]),
        ],
        # Not "staff": Staff Nurse. Attending physicians and (in India and Australia) consultants are senior doctors.
        "skip": [
            ("Senior levels", ["senior", "sr", "principal", "lead", "manager", "mgr", "supervisor", "attending",
                               "consultant"]),
            ("Executives", ["director", "vice president", "vp", "head of", "chief"]),
            ("Other kinds of work", ["sales", "account", "recruiter", "billing", "coder", "coding", "scheduler",
                                     "receptionist", "marketing", "software", "engineer", "analyst", "veterinary", "vet",
                                     "insurance", "claims"]),
        ],
    },
}
for _profession in PROFESSIONS.values():
    _profession["titles"] = [t for _, titles in _profession["groups"] for t in titles]
    _words = [w if isinstance(w, tuple) else (w, w) for _, words in _profession["skip"] for w in words]
    _profession["skip_groups"] = [(name, [w[0] if isinstance(w, tuple) else w for w in words])
                                  for name, words in _profession["skip"]]
    _profession["exclude"] = [pattern for _, pattern in _words]  # the rules saved, all ticked to start with
    _profession["patterns"] = dict(_words)  # label -> rule
    _profession["labels"] = {pattern: label for label, pattern in _words}
assert set(PROFESSIONS) == set(PROFESSION_IDS)

# The countries Setup offers. Picking them decides which companies are checked (their `countries`),
# and, unless the person names cities, which jobs alert: these rules for the job's location text.
_US_STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
              "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
              "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
              "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|"
              "Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia")
_US_CODES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
             "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
_US_CITIES = ("San Francisco|Bay Area|San Jose|Palo Alto|Mountain View|Sunnyvale|Redwood City|Menlo Park|Seattle|"
              "Bellevue|Redmond|Boston|Cambridge|Austin|Dallas|Houston|Chicago|Los Angeles|San Diego|New York City|NYC|"
              "Brooklyn|Atlanta|Denver|Boulder|Portland|Raleigh|Durham|Charlotte|Baltimore|Arlington|McLean|Reston|"
              "Philadelphia|Pittsburgh|Cleveland|Columbus|Cincinnati|Detroit|Ann Arbor|Minneapolis|Madison|San Antonio|"
              "Tampa|Orlando|Miami|Phoenix|Salt Lake City|Nashville|St\\. Louis|Kansas City|Indianapolis")
_CA_PLACES = ("Ontario|Quebec|Québec|British Columbia|Alberta|Manitoba|Saskatchewan|Nova Scotia|New Brunswick|"
              "Newfoundland|Toronto|Vancouver|Montréal|Montreal|Ottawa|Waterloo|Kitchener|Mississauga|Markham|Burnaby|"
              "Calgary|Edmonton|Winnipeg|Halifax")
_IN_PLACES = ("Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Navi Mumbai|Thane|Delhi|New Delhi|Noida|Gurgaon|Gurugram|"
              "Chennai|Kolkata|Ahmedabad|Jaipur|Kochi|Cochin|Thiruvananthapuram|Trivandrum|Chandigarh|Mohali|Indore|"
              "Coimbatore|Mysore|Mysuru|Mangalore|Vadodara|Nagpur|Lucknow|Bhubaneswar|Visakhapatnam|Karnataka|"
              "Maharashtra|Telangana|Tamil Nadu|Haryana|Kerala|Gujarat|Uttar Pradesh|West Bengal")
REMOTE = r"re:^(?:remote|anywhere|worldwide|global)(?:\s*[/,-]\s*(?:remote|anywhere|worldwide|global))*\s*$"
COUNTRIES: dict[str, dict[str, Any]] = {
    "US": {"name": "United States", "locations": [
        "United States", "USA", r"re:\b(?-i:US|USA)\b", rf"re:\b(?:{_US_STATES})\b",
        # A state code after a comma, unless the place is Indian or Canadian ("Pune, IN", "Toronto, CA").
        rf"re:^(?!.*\b(?:{_IN_PLACES}|{_CA_PLACES})\b).*(?:,\s*|\s-\s)(?-i:{_US_CODES})\b",
        rf"re:\b(?:{_US_CITIES})\b(?!,?\s*(?:UK|United Kingdom|England|New Zealand|Germany))"]},
    "CA": {"name": "Canada", "locations": [
        "Canada", r"re:\b(?-i:CAN)\b", r"re:^(?-i:CA)-", r"re:(?:,\s*|\s-\s)(?-i:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b",
        rf"re:\b(?:{_CA_PLACES})\b"]},
    "AU": {"name": "Australia", "locations": [
        "Australia", r"re:\b(?-i:AU|AUS)\b", r"re:(?:,\s*|\s-\s)(?-i:NSW|VIC|QLD|ACT|TAS)\b",
        r"re:\b(?:Sydney|Melbourne|Brisbane|Perth|Adelaide|Canberra|Hobart|Darwin|Gold Coast|Newcastle, NSW|"
        r"New South Wales|Queensland|Tasmania|Western Australia|South Australia)\b"]},
    "IN": {"name": "India", "locations": ["India", r"re:\b(?-i:IND)\b", r"re:^(?-i:IN)-", rf"re:\b(?:{_IN_PLACES})\b"]},
}
_COUNTRY_RULES = {rule for country in COUNTRIES.values() for rule in country["locations"]} | {REMOTE}
MAC_ONLY = {"storage": "sqlite", "secrets": "keychain"}
GMAIL = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls"}
CHECK_TIMEOUT = 45.0  # seconds for one company's check before it counts as not working
ADDRESS = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")  # no commas: EMAIL_TO is a comma-separated list
DISCORD_WEBHOOK = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+")

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


def _write_profile(path: Path, runtime: dict[str, Any], filters: dict[str, Any],
                   settings: dict[str, Any] | None = None) -> None:
    sections = {"runtime": runtime, "filters": filters, **({"settings": settings} if settings else {})}
    text = (f"{GENERATED} Change it in the app (Settings), or by hand.\n"
            "# runtime: where state and alert settings live; filters: what to look for;\n"
            "# settings: the profession (its company list) and countries (which of those companies are checked).\n"
            + yaml.safe_dump(sections, sort_keys=False, allow_unicode=True))
    path.write_text(text, encoding="utf-8")


def _read_yaml(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


# -- show and profile ------------------------------------------------------------------------


def show(config: Path) -> dict[str, Any]:
    from role_radar import keychain

    profile = _read_yaml(profile_path(config))
    filters, settings = profile.get("filters") or {}, profile.get("settings") or {}
    own = _read_yaml(config).get("companies") or []
    try:
        stored = keychain.read_all()
    except RuntimeError:  # not macOS
        stored = {}
    locations = filters.get("locations") or []
    labels = PROFESSIONS[settings["profession"]]["labels"] if settings.get("profession") in PROFESSIONS else {}
    state = {
        "profession": settings.get("profession"),
        "professions": [{"id": pid, "name": p["name"], "about": p["about"],
                         "groups": [{"name": name, "titles": titles} for name, titles in p["groups"]],
                         "skip_groups": [{"name": name, "titles": words} for name, words in p["skip_groups"]]}
                        for pid, p in PROFESSIONS.items()],
        "roles": filters.get("include_keywords") or [],
        "exclude": [labels.get(rule, rule) for rule in filters.get("exclude_keywords") or []],  # as Setup's boxes
        "locations": locations,
        "countries": settings.get("countries") or [],
        "country_options": [{"code": code, "name": c["name"]} for code, c in COUNTRIES.items()],
        "cities": [place for place in locations if place not in _COUNTRY_RULES],
        "max_experience_years": filters.get("max_experience_years"),
        "education": filters.get("education"),
        **_companies(config),
        "own_companies": len(own),
        "email": stored.get("EMAIL_FROM") or stored.get("EMAIL_TO"),
        "also": _also(stored),
        "email_ready": bool(stored.get("EMAIL_TO") and stored.get("SMTP_PASSWORD")),
        "discord_ready": bool(stored.get("DISCORD_WEBHOOK_URL")),
    }
    # Setup is done, and checking may start: with a profession, only once countries narrow its list.
    # Alerts are optional: without them, new matches collect in Live Tracking.
    state["ready"] = bool(state["roles"] and state["companies"]
                          and (state["countries"] or not state["profession"]))
    return state


def _companies(config: Path) -> dict[str, Any]:
    """What checks would read: the profession's companies and their own, in their countries.

    `companies_for` has the count for every combination of countries ("US+IN"), and `sample_for` a few
    of their names, so Setup can show both as countries are ticked; `companies` is for the saved ones.
    """
    raw = combined(config, profile_path(config))
    picked = set((raw.get("settings") or {}).get("countries") or [])
    enabled = [c for c in raw["companies"] if c.get("enabled", True)]
    tracked = lambda chosen: [c for c in enabled if not chosen or not c.get("countries") or chosen & set(c["countries"])]  # noqa: E731
    combos = [[code for i, code in enumerate(COUNTRIES) if mask >> i & 1] for mask in range(1, 2 ** len(COUNTRIES))]
    return {
        "companies": len(tracked(picked)),
        "companies_for": {"+".join(combo): len(tracked(set(combo))) for combo in combos},
        "sample_for": {"+".join(combo): [c["name"] for c in tracked(set(combo)) if c.get("countries")][:12] for combo in combos},
        "companies_by_country": {code: sum(1 for c in enabled if code in (c.get("countries") or [])) for code in COUNTRIES},
        "companies_untagged": sum(1 for c in enabled if not c.get("countries")),
        "companies_off": sum(1 for c in raw["companies"] if not c.get("enabled", True)),
    }


def save_profession(config: Path, profession: str) -> None:
    """Pick a profession: its company list comes with the app, and a new profession brings its own
    titles and rule-out words, keeping any titles the person added themselves."""
    if profession not in PROFESSIONS:
        raise ValueError(f"pick one of: {', '.join(p['name'] for p in PROFESSIONS.values())}")
    path = profile_path(config)
    _check_generated(path)
    profile = _read_yaml(path)
    filters, settings = dict(profile.get("filters") or {}), dict(profile.get("settings") or {})
    if settings.get("profession") != profession:
        offered = {t.casefold() for p in PROFESSIONS.values() for t in p["titles"]}
        theirs = [t for t in filters.get("include_keywords") or [] if t.casefold() not in offered]
        filters.update(include_keywords=PROFESSIONS[profession]["titles"] + theirs,
                       exclude_keywords=list(PROFESSIONS[profession]["exclude"]), match_on=["title"], exclude_on=["title"])
    settings["profession"] = profession
    _write_profile(path, profile.get("runtime") or MAC_ONLY, filters, settings)
    load_config(config, path)  # never leave a profile that doesn't load


def save_profile(config: Path, roles: list[str], exclude: list[str], locations: list[str],
                 max_experience_years: int | None, countries: list[str] | None = None,
                 education: str | None = None) -> None:
    """Replace what the profile looks for; keep where state lives and the profession.

    With `countries` (codes from COUNTRIES), only companies posting there are checked, and `locations`
    are cities: jobs alert from those cities, or anywhere in those countries when none are given,
    plus jobs listed only as "Remote". Without it, `locations` are the location rules themselves.
    `exclude` may name the profession's non-target boxes by their labels. `education` is their
    highest (filters.EDUCATION), and None keeps the saved one.
    """
    path = profile_path(config)
    _check_generated(path)
    clean = lambda words: [w.strip() for w in words if w and w.strip()]  # noqa: E731
    profile = _read_yaml(path)
    settings = dict(profile.get("settings") or {})
    patterns = PROFESSIONS[settings["profession"]]["patterns"] if settings.get("profession") in PROFESSIONS else {}
    filters: dict[str, Any] = {"include_keywords": clean(roles),
                               "exclude_keywords": [patterns.get(w, w) for w in clean(exclude)],
                               "match_on": ["title"], "exclude_on": ["title"]}
    education = education if education is not None else (profile.get("filters") or {}).get("education")
    if education is not None:
        if education not in EDUCATION:
            raise ValueError(f"education must be one of {', '.join(EDUCATION)}")
        filters["education"] = education
    if countries is not None:
        picked = [c for c in COUNTRIES if c in {str(x).upper() for x in countries}]
        if not picked:
            raise ValueError("pick at least one country")
        settings["countries"] = picked
        places = clean(locations) or [rule for c in picked for rule in COUNTRIES[c]["locations"]]
        filters["locations"] = places + [REMOTE]
    elif clean(locations):
        filters["locations"] = clean(locations)
    if max_experience_years is not None:
        if not 0 <= int(max_experience_years) <= 30:
            raise ValueError("years of experience must be between 0 and 30")
        filters["max_experience_years"] = int(max_experience_years)
    if not filters["include_keywords"]:
        raise ValueError("pick at least one job title")
    _write_profile(path, profile.get("runtime") or MAC_ONLY, filters, settings)
    load_config(config, path)  # never leave a profile that doesn't load


def save_email(address: str, app_password: str) -> None:
    """Gmail: send from the address to itself (and anyone added), signing in with an app password
    (spaces don't matter)."""
    from role_radar import keychain

    address, password = address.strip(), re.sub(r"\s+", "", app_password)
    if not ADDRESS.fullmatch(address):
        raise ValueError(f"{address!r} doesn't look like an email address")
    if not re.fullmatch(r"[A-Za-z]{16}", password):
        raise ValueError("a Gmail app password is 16 letters (Google shows it in groups of four)")
    also = _also(keychain.read_all())  # a new Gmail keeps the people added before
    for name, value in {**GMAIL, "EMAIL_TO": _recipients(address, also), "EMAIL_FROM": address,
                        "SMTP_USERNAME": address}.items():
        keychain.write(name, value)
    keychain.write("SMTP_PASSWORD", password)


def save_discord(webhook_url: str) -> None:
    """Discord: alerts go to a channel through a webhook made in its settings (Integrations → Webhooks)."""
    from role_radar import keychain

    url = webhook_url.strip()
    if not DISCORD_WEBHOOK.fullmatch(url):
        raise ValueError("that isn't a Discord webhook URL: in Discord, open the channel's settings, then Integrations → "
                         "Webhooks → New Webhook → Copy Webhook URL (it starts https://discord.com/api/webhooks/)")
    keychain.write("DISCORD_WEBHOOK_URL", url)


def save_recipients(also: list[str]) -> None:
    """Who else gets the alerts, besides the Gmail address they come from (which always does)."""
    from role_radar import keychain

    sender = keychain.read("EMAIL_FROM")
    if not sender:
        raise ValueError("save your Gmail address and app password first")
    keychain.write("EMAIL_TO", _recipients(sender, also))


def _recipients(sender: str, also: list[str]) -> str:
    """EMAIL_TO: the sender first, then everyone else once each."""
    found, seen = [sender], {sender.casefold()}
    for address in (a.strip() for a in also):
        if not address:
            continue
        if not ADDRESS.fullmatch(address):
            raise ValueError(f"{address!r} doesn't look like an email address")
        if address.casefold() not in seen:
            seen.add(address.casefold())
            found.append(address)
    return ",".join(found)


def _also(stored: dict[str, str]) -> list[str]:
    """Everyone the alerts go to besides the sender."""
    sender = (stored.get("EMAIL_FROM") or "").casefold()
    return [r.strip() for r in (stored.get("EMAIL_TO") or "").split(",") if r.strip() and r.strip().casefold() != sender]


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
    """Known employers with verified job boards, by name key: the professions' company lists, or the
    file at `path` ($ROLE_RADAR_DIRECTORY). Their own filters are left behind."""
    path = path or os.environ.get("ROLE_RADAR_DIRECTORY")
    if path:
        entries = _read_yaml(Path(path)).get("companies") or [] if Path(path).exists() else []
    else:
        entries = [e for pid in PROFESSIONS for e in profession_list(pid)]
    return {_key(e["name"]): {k: v for k, v in e.items() if k != "filters"} for e in entries
            if e.get("name") and e.get("url") and e.get("enabled", True)}


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
