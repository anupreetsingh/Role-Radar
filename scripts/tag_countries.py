"""Write each company's `countries` (where it posts jobs) into a companies file.

Run from the project root, with job locations from either source:
  python -m scripts.tag_countries --export DIR    # a DynamoDB export of the state table (DYNAMODB_JSON)
  python -m scripts.tag_countries --jobs FILE     # JSON: [{"company": ..., "location": ...}, ...]
Add --dry-run to see the counts without writing.

A company gets every target country (US, CA, AU, IN) that at least one of its jobs, open or
closed, was posted in (scripts/countries.py reads the location text). A company the jobs
don't place in any target country keeps whatever `countries` it has, or none, and a company
without `countries` is checked whichever countries are picked. Only `countries:` lines
change: comments and layout stay as they are.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from scripts.countries import TARGETS, default_classifier

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def export_jobs(directory: Path) -> Iterator[tuple[str, str | None]]:
    """(company, location) for every saved job in a DynamoDB export of the state table."""
    for path in sorted(directory.rglob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                item = json.loads(line)["Item"]
                company = item["pk"]["S"]
                if not company.startswith("#"):  # the store's own rows
                    yield company, (item.get("location") or {}).get("S")


def file_jobs(path: Path) -> Iterator[tuple[str, str | None]]:
    for row in json.loads(path.read_text(encoding="utf-8")):
        yield row["company"], row.get("location")


def countries_by_company(jobs: Iterable[tuple[str, str | None]]) -> dict[str, Counter]:
    """How many jobs each company posted in each country."""
    classify = default_classifier().countries
    found: dict[str, Counter] = defaultdict(Counter)
    for company, location in jobs:
        found[company].update(classify(location))
    return found


def write_tags(text: str, tags: dict[str, list[str]]) -> str:
    """`text` with a `countries:` line for each company in `tags`, after its url (and ats) line."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"  - name: (.*)$", lines[i].rstrip("\n"))
        if not m:
            out.append(lines[i])
            i += 1
            continue
        block = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].startswith("    "):
            block.append(lines[i])
            i += 1
        codes = tags.get(str(yaml.load("x: " + m.group(1), Loader=LOADER)["x"]))
        if codes:
            block = [b for b in block if not b.startswith("    countries:")]
            at = next(k for k, b in enumerate(block) if b.startswith("    url:")) + 1
            if at < len(block) and block[at].startswith("    ats:"):
                at += 1
            block.insert(at, f"    countries: [{', '.join(codes)}]\n")
        out.extend(block)
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--export", type=Path, help="directory of a DynamoDB export of the state table")
    source.add_argument("--jobs", type=Path, help='JSON list of {"company", "location"}')
    parser.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    found = countries_by_company(export_jobs(args.export) if args.export else file_jobs(args.jobs))
    text = args.config.read_text(encoding="utf-8")
    before = yaml.load(text, Loader=LOADER)["companies"]
    tags = {str(c["name"]): [k for k in TARGETS if found.get(str(c["name"]), Counter())[k]] for c in before}
    tags = {name: codes for name, codes in tags.items() if codes}
    new_text = write_tags(text, tags)

    # Nothing but `countries` may change.
    after = yaml.load(new_text, Loader=LOADER)["companies"]
    strip = lambda c: {k: v for k, v in c.items() if k != "countries"}  # noqa: E731
    assert [strip(c) for c in after] == [strip(c) for c in before], "a change beyond countries"
    for c in after:
        assert c.get("countries") == tags.get(str(c["name"]), c.get("countries")), c["name"]

    per = {k: sum(k in (c.get("countries") or []) for c in after) for k in TARGETS}
    untagged = [c["name"] for c in after if not c.get("countries")]
    print(f"{len(after):,} companies: " + ", ".join(f"{k} {n:,}" for k, n in per.items())
          + f"; {len(untagged):,} without countries (checked for everyone)")
    if not args.dry_run:
        args.config.write_text(new_text, encoding="utf-8")
        print(f"Wrote {args.config}")


if __name__ == "__main__":
    main()
