"""Check enabled career sources without state writes or notifications.

Run from the project root: python -m scripts.validate_companies --output PATH
The JSON report records a timestamp and config hash so an old check cannot be
mistaken for validation of a newer company list.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from role_radar.config import load_config
from role_radar.http_client import HttpClient
from role_radar.scrapers import scraper_class_for


async def validate(path: Path, only: set[str]) -> dict:
    config = load_config(path)
    gate = asyncio.Semaphore(12)
    async with HttpClient(config.settings.http) as http:
        async def check(company):
            async with gate:
                row = {"name": company.name, "source": company.ats or scraper_class_for(company.url).name}
                try:
                    async def fetch():
                        scraper = scraper_class_for(company.url, company.ats)(company, http)
                        result = await scraper.fetch_jobs()
                        matched, unresolved, details = 0, 0, 0
                        for job in result.jobs:
                            unknown = scraper.missing_fields(job)
                            if not company.filter.could_match(job, unknown):
                                continue
                            if scraper.wants_details(job):
                                if details >= config.settings.max_detail_requests:
                                    unresolved += 1
                                    continue
                                job = await scraper.fetch_details(job)
                                details += 1
                            matched += bool(company.filter.evaluate(job))
                        return result, matched, unresolved
                    result, matched, unresolved = await asyncio.wait_for(fetch(), timeout=config.settings.company_timeout)
                    row.update(status="ok", listings=len(result.jobs), matches=matched,
                               complete_snapshot=result.complete, pending_detail_checks=unresolved)
                except Exception as exc:
                    row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                print(json.dumps(row, ensure_ascii=False), flush=True)
                return row
        rows = await asyncio.gather(*(check(c) for c in config.companies if c.enabled and (not only or c.name in only)))
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "companies": sorted(rows, key=lambda r: r["name"].casefold()),
            "requests": http.stats.requests,
            "all_sources_ok": bool(rows) and all(r["status"] == "ok" for r in rows),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--company", action="append", default=[])
    args = parser.parse_args()
    report = asyncio.run(validate(args.config, set(args.company)))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0 if report["all_sources_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
