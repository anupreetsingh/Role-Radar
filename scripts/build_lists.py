"""Add the employers a board sweep found to each profession's company list.

Run from the project root:
  python -m scripts.build_lists --results data/sweep/results.jsonl [--names data/sweep/names.json]
                                [--export DIR] [--dry-run]

--results is a board sweep (scripts/sweep_boards.py): every board's job titles and locations.
--names maps a board URL to its page title, for boards the sweep couldn't name (Oracle, Eightfold).
--export is a DynamoDB export of the state table (as for scripts/tag_countries.py): the saved jobs
of the companies already on the Tech list, so those also hiring for another profession join its list.

A company joins a profession's list when at least one of its jobs has one of that profession's
titles (onboarding.PROFESSIONS, less its "other kinds of work"), in one of the target countries
(US, CA, AU, IN). Its `countries` are every target country any of its jobs is in. Left out:
companies already on the list (by URL or name), staffing agencies and job sites, and, for Tech,
the employers removed before (docs/company-coverage.md) and boards whose tech jobs mostly need a
security clearance. Tech's list is config/companies.yaml, where they're added in a section at the
end; the others are config/accounting.yaml and config/healthcare.yaml, written whole.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

import yaml

from role_radar.filters import JobFilter
from role_radar.models import JobPosting, normalize_url, slugify
from role_radar.onboarding import PROFESSIONS
from role_radar.scrapers import ats_name
from scripts.countries import TARGETS, default_classifier

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
LIST_FILES = {"tech": "companies.yaml", "accounting": "accounting.yaml", "healthcare": "healthcare.yaml"}
ABOUT = {
    "accounting": "accounting and finance jobs (accountants, audit, tax, FP&A, payroll)",
    "healthcare": "healthcare jobs (nurses, doctors, pharmacists, therapists, technologists)",
}
# Readers that search for tech roles themselves, or need tech-specific options: never copied
# from the Tech list to another profession's.
TECH_ONLY_READERS = {"amazon", "apple", "google", "meta", "tiktok", "mathworks", "comsol", "generic"}
TECH_ONLY_OPTIONS = {"categories", "query", "keyword"}

STAFFING = re.compile(
    r"\b(?:staffing|recruit(?:ing|ment|ers)?|personnel|manpower|headhunt\w*|placements?|"
    r"talent (?:solutions|partners|group|acquisition)|workforce (?:solutions|group)|employment (?:agency|services)|"
    r"temp(?:orary)? (?:services|help)|executive search|search partners|locum\w*|travel nurs\w*|"
    r"job ?board|jobs? network|hiring platform)\b", re.I)
STAFFING_NAMES = re.compile(
    r"^(?:randstad|adecco|kelly services|robert half|manpowergroup|aerotek|teksystems|allegis|insight global|"
    r"kforce|hays|michael page|pagegroup|cross country|amn healthcare|aya healthcare|medical solutions|"
    r"trusted health|vivian|incredible health|nomad health|fastaff|soliant|comphealth|maxim healthcare|"
    r"totalmed|supplemental health care|accountemps|apex systems|collabera|jobot|dice|indeed|ziprecruiter|"
    r"hired|monster(?:\.com)?$|glassdoor|vettery|triplebyte|toptal|upwork|fiverr|andela|turing|crossover|"
    r"medely|clipboard health|shiftkey|nursa|intelycare|onin|atc healthcare|ingenovis|fusion medical)\b", re.I)
# Employers whose names only look like an agency's.
NOT_STAFFING = re.compile(r"^(?:government of|city of|county of|state of|blue origin|allsup)\b", re.I)
# Employers taken off the Tech list before (docs/company-coverage.md: defense, aerospace and space;
# the resume-alignment removals): distinctive names match at the start, short ones only whole.
REMOVED_FROM_TECH = re.compile(
    r"^(?:(?:lockheed|northrop|raytheon|general dynamics|l3harris|leonardo drs|bae systems|leidos|science applications|"
    r"booz allen|mantech|peraton|huntington ingalls|textron|sierra nevada|kratos|mercury systems|anduril|"
    r"spacex|space exploration tech|blue origin|boeing|aerojet|united launch|technology service corporation|"
    r"amentum|vectrus|teledyne|vishay|texas instruments|marvell|globalfoundries|onsemi|zt systems|form energy|"
    r"lhp engineering|vertiv|cummins|carrier global|ge appliances|hologic|becton|ul solutions|keybank|"
    r"texas capital|great american insurance|integrity marketing|aaa club|highmark|raymond james|"
    r"federal reserve|albertsons)|"
    r"(?:rtx|saic|caci|v2x|parsons|moog|coherent|nxp|eaton|denso|adt|citizens|pimco|ascensus|gartner)$)", re.I)
CLEARANCE = re.compile(r"\b(?:clearance|cleared|TS/SCI|top secret|secret|polygraph|SCI)\b", re.I)

# -- names ----------------------------------------------------------------------------------------

_GENERIC_SEGMENT = re.compile(
    r"^(?:(?:search |current |open |all )?(?:jobs?|careers?|roles|positions|openings|opportunities)"
    r"(?: (?:search|listings?|opportunities|openings|center|centre|site|page|board|home))?|"
    r"employment|human resources|home|welcome|job search|error \d+.*|page not found|not found|"
    r"candidate experience(?: page| site)?(?: .*)?|careers? site|oracle.*|workday|icims)$", re.I)
_AT = re.compile(r"\b(?:job listings|jobs|careers?|(?:current |career |job )?(?:opportunities|openings)|open roles|join us|"
                 r"work|our team|(?:your )?career journey) (?:at|with)\s+(.+)$", re.I)
_LEADING = re.compile(r"^(?:[*\W]+|(?:careers?|jobs?|join)\s+(?:at|with)?\s*|candidate experience site\s+|the (?=[A-Z]))", re.I)
_TRAILING = re.compile(
    r"(?:\s+|'s |’s )(?:jobs?|careers?|careers? (?:page|site|center|centre|portal|section|search agents)|job board|"
    r"jobs page|openings|opportunities|website|external(?: careers?)?(?: site)?|portal|hiring|open positions|"
    r"talent acquisition(?: site)?|candidate(?: experience)? site|jobs for \w+)$|\s+(?:and|&) (?:become|join) .*$|[!:]+$", re.I)
_LEGAL = re.compile(
    r",?\s+(?:inc\.?|incorporated|llc|l\.l\.c\.|ltd\.?|limited|private limited|pvt\.?(?: ltd\.?)?|corp\.?|"
    r"corporation|co\.|gmbh|s\.a\.?|sau|plc|llp|l\.l\.p\.|lp|l\.p\.|pllc|p\.c\.|pc|n\.a\.|ag|b\.v\.|"
    r"pty\.?(?: ltd\.?)?|pte\.?(?: ltd\.?)?|\((?:usa|us|u\.s\.|india|canada|australia)\))$", re.I)
# Workday's legal entities often start with an internal code: "10 Manhattan Associates", "F19063 Keller Oaks",
# and end with the country of the entity: "Canada Goose, US".
_WORKDAY_CODE = re.compile(r"^(?:[A-Z]{1,4}[ ._-]?\d+|\d{2,})[\w.]*\s+(?!Hour\b)(?=\S)")
_WORKDAY_CODE_PART = re.compile(r"^(?:[A-Z]{1,5}|[A-Z]{0,4}[ ._-]?\d[\w.]*(?: [A-Z]{2,4})?|US|USA|UK|India|INDIA|Canada|Australia)$")
_WORKDAY_COUNTRY = re.compile(r"(?<=\w),?\s+(?:US|USA|U\.S\.)$")
# What's left of a page title that names no one: the board's URL names it instead.
_NO_NAME = re.compile(
    r"^(?:about us|contact(?: us)?|us|for|apply(?: for| online)?|our team|search|home care|jobs?|careers?|"
    r"icims.*|candidate experience.*|build your future.*|opportunities to join.*|shoreside positions|"
    r"team member services|people services|& culture|site organic|providers|for \w+|campus|lateral|interns?|"
    r"internal|external|experienced|students?|graduates?)$", re.I)


def clean_name(raw: str | None, platform: str = "") -> str:
    """A company's name from a page title or legal entity: "Job Listings at Quartz" → "Quartz",
    "Careers | Covenant House New York" → "Covenant House New York", "Smokeball Careers" → "Smokeball",
    and on Workday "10 Manhattan Associates, Inc." → "Manhattan Associates". "" when it names no one."""
    text = re.sub(r"[\s_]+", " ", (raw or "").replace("\\'", "'").replace("\\/", "/")).strip()
    segments = [s.strip() for s in re.split(r"\s+[|–—·•:-]+\s*", text) if s.strip()]
    at = [m[1] for s in segments if (m := _AT.search(s))]
    if platform == "workday":  # "001 - Illinois Tool Works Inc.", "6250 - India - Avantor ..."
        segments = [s for s in segments if not _WORKDAY_CODE_PART.match(s)] or segments
    specific = [s for s in segments if (rest := _TRAILING.sub("", s)) and not _GENERIC_SEGMENT.match(rest)]
    name = (at[-1:] or specific or [""])[0]
    name = _LEADING.sub("", name)
    if platform == "workday":
        name = _WORKDAY_CODE.sub("", name)
    for pattern in (_TRAILING, _LEGAL, _LEGAL, _TRAILING):
        name = pattern.sub("", name).strip(" ,.-")
    if platform == "workday":
        name = _WORKDAY_COUNTRY.sub("", name)
    if name.isupper() and len(name.split()) > 1:  # "HOME DEPOT CANADA" → "Home Depot Canada"; short words may be acronyms
        name = " ".join(w.title() if w.isalpha() and len(w) > 3 else w for w in name.split())
    return "" if _NO_NAME.match(name) else name


def slug_name(url: str) -> str:
    """A name from the board's URL when the board has none: "https://jobs.ashbyhq.com/lumen.energy" → "Lumen Energy"."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
    if host.endswith("icims.com"):  # {portal}-{company}.icims.com
        slug = host.split(".")[0].split("-", 1)[-1]
    elif any(h in host for h in ("myworkdayjobs.com", "eightfold.ai", "oraclecloud.com")):
        slug = host.split(".")[0]
    elif "myworkdaysite.com" in host and len(segments) > 1:
        slug = segments[1]
    else:
        slug = segments[0] if segments else host.split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_. ]+", slug) if w)


# -- boards ---------------------------------------------------------------------------------------


@dataclass
class Board:
    url: str
    platform: str
    name: str
    jobs: list[tuple[str, str | None]]
    total: int
    options: dict[str, Any] = field(default_factory=dict)
    listed: bool = False  # on the Tech list already (the sweep re-read it)

    @property
    def site(self) -> str:
        return company_site(self.url, self.platform)


# One company's job site can have several boards (Workday sites, Oracle sites); those of a site
# already on a list would repeat its jobs.
SHARED_SITES = {"workday", "oracle_hcm", "eightfold", "icims"}


def company_site(url: str, platform: str | None) -> str:
    """The company's own site on a shared job platform ("acme" of acme.wd5.myworkdayjobs.com), else the URL."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if "myworkdaysite.com" in host and len(parts.path.split("/")) > 2:
        return f"workday:{parts.path.split('/')[2].lower()}"
    if platform in SHARED_SITES:
        return f"{platform}:{host.split('.')[0].lower() if platform == 'workday' else host.lower()}"
    return normalize_url(url)


def sweep_boards(results: Path, names: dict[str, str | None]) -> Iterator[Board]:
    """Each board the sweep read, with its jobs (listing and searches together)."""
    for line in results.open(encoding="utf-8"):
        row = json.loads(line)
        if not row.get("ok"):
            continue
        jobs = [tuple(j) for j in row.get("jobs") or []]
        for search in (row.get("searches") or {}).values():
            jobs += [tuple(j) for j in search.get("jobs") or []]
        raw = row.get("name") or names.get(row["url"])
        # An Oracle site's URL names no one ("ebhu.fa.us2.oraclecloud.com"): without a title it gets no name.
        name = clean_name(raw, row["platform"]) or ("" if row["platform"] == "oracle_hcm" else slug_name(row["url"]))
        yield Board(url=row["url"], platform=row["platform"], name=name,
                    jobs=list(dict.fromkeys(jobs)), total=row.get("total") or len(row.get("jobs") or []),
                    options=row.get("options") or {}, listed=bool(row.get("company")))


def export_jobs(directory: Path) -> dict[str, list[tuple[str, str | None]]]:
    """Company name → (title, location) of every job saved in a DynamoDB export of the state table."""
    found: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for path in sorted(directory.rglob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                item = json.loads(line)["Item"]
                company = item["pk"]["S"]
                if not company.startswith("#") and "title" in item:
                    found[company].append((item["title"]["S"], (item.get("location") or {}).get("S")))
    return found


# -- matching -------------------------------------------------------------------------------------


class Matcher:
    """Which target countries a board's jobs are in, overall and for one profession's titles."""

    def __init__(self) -> None:
        self.countries = default_classifier().countries
        self.filters = {}
        for key, profession in PROFESSIONS.items():
            other = [w if isinstance(w, str) else w[1] for group, words in profession["skip"]
                     if group == "Other kinds of work" for w in words]
            self.filters[key] = JobFilter(include_keywords=profession["titles"], exclude_keywords=other)

    def places(self, location: str | None) -> list[str]:
        return [c for c in self.countries(location) if c in TARGETS]

    def matches(self, profession: str, jobs: Iterable[tuple[str, str | None]]) -> list[tuple[str, list[str]]]:
        """(title, target countries) of each job with one of the profession's titles in a target country."""
        out = []
        for title, location in jobs:
            places = self.places(location)
            job = JobPosting(company="-", title=title or "", url="", source="-", location=location)
            if places and self.filters[profession].evaluate(job).matched:
                out.append((title, places))
        return out

    def all_countries(self, jobs: Iterable[tuple[str, str | None]]) -> list[str]:
        found = {c for _, location in jobs for c in self.places(location)}
        return [c for c in TARGETS if c in found]


def left_out(profession: str, name: str, matched: list[tuple[str, list[str]]]) -> str | None:
    """Why a company stays off the profession's list, or None."""
    if (STAFFING.search(name) or STAFFING_NAMES.search(name)) and not NOT_STAFFING.search(name):
        return "staffing agency or job site"
    if profession == "tech" and REMOVED_FROM_TECH.search(name):
        return "removed from the Tech list before"
    if profession == "tech" and sum(bool(CLEARANCE.search(t)) for t, _ in matched) * 2 >= len(matched):
        return "tech jobs mostly need a clearance"
    return None


def entry(board: Board, countries: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"name": board.name, "url": board.url, "ats": board.platform, "countries": countries}
    options = dict(board.options)
    if board.platform == "workday":
        options = {"max_jobs": 200, **options}
    if board.platform == "eightfold" and set(countries) - {"US"}:
        options = {"location": "", **options}  # every country, not the reader's default "United States"
    if options:
        out["options"] = options
    return out


def pick(profession: str, boards: list[Board], known_sites: set[str], known_names: set[str],
         matcher: Matcher, skipped: Counter) -> list[dict[str, Any]]:
    """New entries for the profession, one per board; a name several boards share goes to the one with the most matches."""
    best: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for board in boards:
        if board.listed or board.site in known_sites:
            skipped["its site is on the list already"] += not board.listed
            continue
        matched = matcher.matches(profession, board.jobs)
        if not matched:
            continue
        reason = left_out(profession, board.name, matched)
        if reason:
            skipped[reason] += 1
            continue
        key = slugify(board.name)
        if not key or key in known_names:
            skipped["already listed by name" if key else "no name"] += 1
            continue
        rank = (len(matched), board.total)
        if key not in best or rank > best[key][:2]:
            if key in best:
                skipped["another board of the same name"] += 1
            best[key] = (*rank, entry(board, matcher.all_countries(board.jobs)))
        else:
            skipped["another board of the same name"] += 1
    return sorted((e for *_, e in best.values()), key=lambda e: e["name"].lower())


def from_tech_list(profession: str, tech: list[dict[str, Any]], saved: dict[str, list], matcher: Matcher,
                   skipped: Counter) -> list[dict[str, Any]]:
    """Tech-list companies whose saved jobs include the profession's titles, copied as they are."""
    out = []
    for company in tech:
        jobs = saved.get(str(company["name"]))
        if not jobs or company.get("enabled") is False:
            continue
        reader = company.get("ats") or ats_name(company["url"])
        if reader in TECH_ONLY_READERS or TECH_ONLY_OPTIONS & set(company.get("options") or {}):
            skipped["Tech-list reader searches for tech roles"] += bool(matcher.matches(profession, jobs))
            continue
        matched = matcher.matches(profession, jobs)
        if matched and not left_out(profession, str(company["name"]), matched):
            out.append({k: v for k, v in company.items() if k != "filters"})
    return out


# -- writing --------------------------------------------------------------------------------------


def dump(entries: list[dict[str, Any]]) -> str:
    """Entries in the companies file's layout: two-space list items, `countries: [US, IN]`, options
    as a block, and a blank line between entries."""
    blocks = []
    for e in entries:
        lines = []
        for key, value in e.items():
            if key == "countries":
                lines.append(f"countries: [{', '.join(value)}]")
            else:
                text = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=200, default_flow_style=False)
                lines += text.rstrip("\n").splitlines()
        blocks.append("  - " + "\n    ".join(lines) + "\n")
    return "\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--names", type=Path)
    parser.add_argument("--export", type=Path, help="directory of a DynamoDB export of the state table")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--date", default="2026-09-30", help="when the sweep ran, for the files' notes")
    parser.add_argument("--found", default="Boards on the job sites Role Radar reads, from Common Crawl's index, each read once\n"
                        "from AWS.", help="where the boards came from and how they were read, for the Tech list's note")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    names = json.loads(args.names.read_text(encoding="utf-8")) if args.names else {}
    boards = list(sweep_boards(args.results, names))
    saved = export_jobs(args.export) if args.export else {}
    matcher = Matcher()
    tech_path = args.config / LIST_FILES["tech"]
    tech_text = tech_path.read_text(encoding="utf-8")
    tech = yaml.load(tech_text, Loader=LOADER)["companies"]

    for profession, filename in LIST_FILES.items():
        path = args.config / filename
        current = tech if profession == "tech" else (
            (yaml.load(path.read_text(encoding="utf-8"), Loader=LOADER) or {}).get("companies") or [] if path.exists() else [])
        skipped: Counter = Counter()
        carried = [] if profession == "tech" else from_tech_list(profession, tech, saved, matcher, skipped)
        carried = [c for c in carried if slugify(str(c["name"])) not in {slugify(str(e["name"])) for e in current}]
        known = current + carried
        sites = {company_site(c["url"], c.get("ats") or ats_name(c["url"])) for c in known}
        added = pick(profession, boards, sites, {slugify(str(c["name"])) for c in known}, matcher, skipped)
        per = Counter(c for e in carried + added for c in e.get("countries") or [])
        platforms = Counter(e.get("ats") or ats_name(e["url"]) for e in added)
        print(f"{profession}: {len(added):,} new from the sweep, {len(carried):,} from the Tech list; "
              f"by country {dict(per)}; by site {dict(platforms.most_common())}; left out {dict(skipped)}")
        if args.dry_run:
            continue
        if profession == "tech":
            found = "".join(f"  # {line}\n" for line in args.found.splitlines())
            section = (f"\n  # --- Employers found by the board sweep ({args.date}) ---\n" + found +
                       f"  # Kept if a job had a tech title in the US, Canada, Australia or India.\n"
                       f"  # Built by scripts/build_lists.py; see docs/company-coverage.md.\n\n"
                       + dump(added) + "\n  # --- End of employers found by the board sweep ---\n")
            path.write_text(tech_text.rstrip("\n") + "\n" + section, encoding="utf-8")
        else:
            header = (f"# Role Radar's {PROFESSIONS[profession]['name']} companies: employers that posted {ABOUT[profession]}\n"
                      f"# in the US, Canada, Australia or India. Each company's `countries` are where its jobs are,\n"
                      f"# and a person's picked countries decide which are checked. Built by scripts/build_lists.py\n"
                      f"# from a board sweep ({args.date}) and the Tech list's saved jobs; see docs/company-coverage.md.\n\n"
                      "companies:\n")
            path.write_text(header + dump(sorted(known + added, key=lambda e: str(e["name"]).lower())), encoding="utf-8")
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
