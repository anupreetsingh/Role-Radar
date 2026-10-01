"""Read many job boards once each, keeping every job's title and location, to build company lists.

Run it away from home internet (docs/company-coverage.md explains how it was run on AWS):
  python -m scripts.sweep_boards BOARDS.json RESULTS.jsonl

BOARDS.json is [{"url": ..., "platform": ..., "options": {...}}, ...]: platform is a Role Radar
reader name, and options (optional) its reader settings, e.g. {"country": ""} for Amazon.
RESULTS.jsonl gets one line per board, and a rerun skips the boards already in it:
  {"url", "platform", "ok", "error", "name", "total", "jobs": [[title, location], ...],
   "searches": {"engineer": {"total", "jobs"}, ...}, "facets": {name: [[value, count], ...]}}

Greenhouse, Ashby, Lever, Rippling, BambooHR, SmartRecruiters and Workable boards are read whole
(SmartRecruiters' big location groups only as far as two "Show more jobs" pages). Workday, iCIMS, Oracle and Eightfold boards can hold thousands, so they're read as the
newest page plus one page of results for each word in SEARCHES (one word per search: Workday
finds only jobs matching every word of a search). Pacing is the app's own (settings.http in
config/companies.yaml), with iCIMS portals sharing one spacing. When a platform's boards
answer "too many requests", that platform slows down; after the third time it stops.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import yaml

from role_radar.config import CompanyConfig, parse_raw
from role_radar.filters import JobFilter
from role_radar.http_client import HttpClient
from role_radar.models import html_to_text
from role_radar.scrapers import scraper_class_for
from scripts.countries import TARGETS, default_classifier

SEARCHES = ("engineer", "developer", "nurse", "accountant")
WHOLE = {"greenhouse", "ashby", "lever", "rippling", "bamboohr", "smartrecruiters", "workable"}
WORKERS = {"greenhouse": 12, "ashby": 12, "lever": 4, "rippling": 6, "bamboohr": 6, "oracle_hcm": 8,
           "eightfold": 1, "icims": 4, "workday": 4, "smartrecruiters": 4, "workable": 4}
# Reader settings for a sweep, where a sample of a board's titles is enough.
SWEEP_OPTIONS = {"smartrecruiters": {"max_more_pages": 2}}
EXTRA_DELAYS = {"icims.com": 0.5, "jobs.ashbyhq.com": 0.5, "jobs.lever.co": 0.5}
STOP_AFTER_429 = 3


class CountingClient(HttpClient):
    """Counts "too many requests" answers per platform, and slows that platform's hosts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.limited: Counter = Counter()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = await super()._send(method, url, **kwargs)
        if resp.status_code == 429:
            host = urlsplit(url).hostname or ""
            self.limited[platform_of(host)] += 1
            key = self.settings.throttle_key(host)
            self.settings.host_delays[key] = max(1.0, self.settings.delay_for(host) * 2)
            print(f"429 from {host}: {key} now {self.settings.host_delays[key]:.1f}s apart", file=sys.stderr, flush=True)
        return resp


def platform_of(host: str) -> str:
    for part, name in (("myworkday", "workday"), ("icims.com", "icims"), ("greenhouse.io", "greenhouse"),
                       ("ashbyhq.com", "ashby"), ("lever.co", "lever"), ("rippling.com", "rippling"),
                       ("bamboohr.com", "bamboohr"), ("oraclecloud.com", "oracle_hcm"), ("eightfold.ai", "eightfold"),
                       ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable")):
        if part in host:
            return name
    return host


def pairs(jobs: list) -> list[list[str | None]]:
    return [[j.title, j.location] for j in jobs]


def title_of(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return html_to_text(m[1]).strip() or None if m else None


def facets(raw: list[dict[str, Any]] | None) -> dict[str, list[list[Any]]]:
    """Workday's facets flattened: {"locations": [["Austin, Texas, United States", 3], ...], ...}."""
    out: dict[str, list[list[Any]]] = {}

    def walk(items: list[dict[str, Any]], param: str | None) -> None:
        for item in items or []:
            if "count" in item and param:
                out.setdefault(param, []).append([item.get("descriptor"), item.get("count")])
            elif isinstance(item.get("values"), list):
                walk(item["values"], item.get("facetParameter") or param)

    walk(raw or [], None)
    return out


class Sweep:
    def __init__(self, http: CountingClient) -> None:
        self.http = http
        self.classify = default_classifier().countries

    def reader(self, url: str, platform: str, options: dict[str, Any] | None = None, board: dict[str, Any] | None = None):
        # The sweep's limits for the platform, the board's own settings, then this read's.
        options = {**SWEEP_OPTIONS.get(platform, {}), **((board or {}).get("options") or {}), **(options or {})}
        cfg = CompanyConfig(name=url, url=url, filter=JobFilter(), ats=platform, options=options)
        return scraper_class_for(url, platform)(cfg, self.http)

    def in_targets(self, rows: list[list[str | None]]) -> bool:
        """Whether a board has a job that is (or might be) in one of the four countries."""
        return any(not (c := self.classify(loc)) or c & set(TARGETS) for _, loc in rows)

    async def read(self, board: dict[str, Any]) -> dict[str, Any]:
        url, platform = board["url"], board["platform"]
        out: dict[str, Any] = {"url": url, "platform": platform, "ok": False}
        if board.get("company"):  # a listed company read again, e.g. without its reader's country limit
            out["company"] = board["company"]
        try:
            await getattr(self, f"_{platform}", self._whole)(url, platform, out, board)
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, none stops the sweep
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out

    async def _whole(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        result = await self.reader(url, platform, board=board).fetch_jobs()
        out["jobs"] = pairs(result.jobs)
        out["total"] = len(result.jobs)
        if out["jobs"] and self.in_targets(out["jobs"]):
            out["name"] = await self._name(url, platform)

    async def _workable(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        """The widget feed has the account's name beside its jobs, so one request does both."""
        reader = self.reader(url, platform, board=board)
        data = await self.http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{reader.account}")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("unexpected Workable response shape")
        jobs = [reader.parse_job(item) for item in data["jobs"] if item.get("shortcode")]
        out.update(jobs=pairs(jobs), total=len(jobs), name=data.get("name"))

    async def _name(self, url: str, platform: str) -> str | None:
        try:
            if platform == "greenhouse":
                token = self.reader(url, platform).board_token
                return (await self.http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}")).get("name")
            return title_of(await self.http.get_text(url))
        except Exception:  # noqa: BLE001 - a name can be looked up later
            return None

    async def _workday(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        wd = self.reader(url, platform, board=board)

        async def query(text: str) -> tuple[dict[str, Any], list]:
            data = await self.http.post_json(f"{wd.api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": text})
            posts = [p for p in (data or {}).get("jobPostings") or [] if p.get("externalPath")]
            return data or {}, [wd.parse_listing(p) for p in posts]

        data, jobs = await query("")
        out.update(total=data.get("total"), jobs=pairs(jobs), facets=facets(data.get("facets")), searches={})
        if not jobs:
            return
        for word in SEARCHES:
            found, found_jobs = await query(word)
            out["searches"][word] = {"total": found.get("total"), "jobs": pairs(found_jobs)}
        detail = await self.http.get_json(f"{wd.api}{jobs[0].extra['path']}")
        out["name"] = ((detail or {}).get("hiringOrganization") or {}).get("name")

    async def _searched(self, url: str, platform: str, out: dict[str, Any], newest: dict[str, Any],
                        search: Any, board: dict[str, Any]) -> None:
        result = await self.reader(url, platform, newest, board).fetch_jobs()
        out.update(jobs=pairs(result.jobs), total=len(result.jobs), searches={})
        if not result.jobs:
            return
        for word in SEARCHES:
            found = await search(word)
            out["searches"][word] = {"total": len(found.jobs), "jobs": pairs(found.jobs)}

    async def _icims(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        async def search(word: str):
            return await self.reader(f"{url}?{urlencode({'searchKeyword': word})}", platform, {"max_pages": 1}, board).fetch_jobs()

        await self._searched(url, platform, out, {"max_pages": 1}, search, board)
        if out["jobs"] and self.in_targets(out["jobs"]):
            out["name"] = await self._name(url, platform)

    async def _oracle_hcm(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        async def search(word: str):
            return await self.reader(url, platform, {"keyword": word, "max_jobs": 25, "page_size": 25}, board).fetch_jobs()

        await self._searched(url, platform, out, {"max_jobs": 100}, search, board)

    async def _eightfold(self, url: str, platform: str, out: dict[str, Any], board: dict[str, Any]) -> None:
        api = {"location": ""}  # every country, not the reader's default "United States"
        try:
            await self.reader(url, platform, {**api, "max_jobs": 10}, board).fetch_jobs()
        except Exception as exc:  # noqa: BLE001 - a tenant without the pcsx API answers 403 or 404
            if not re.search(r"HTTP 40[34]", str(exc)):
                raise
            api["api"] = "apply"
            out["api"] = "apply"

        async def search(word: str):
            return await self.reader(url, platform, {**api, "query": word, "max_jobs": 10}, board).fetch_jobs()

        await self._searched(url, platform, out, {**api, "max_jobs": 50}, search, board)


async def run(boards: list[dict[str, Any]], results: Path) -> None:
    done = set()
    if results.exists():
        done = {json.loads(line)["url"] for line in results.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for board in boards:
        if board["url"] not in done:
            todo[board["platform"]].append(board)
    raw = yaml.load(Path("config/companies.yaml").read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    settings = parse_raw({**raw, "companies": []}).settings
    settings.http.host_delays.update(EXTRA_DELAYS)
    settings.http.max_concurrency = 48
    counts: Counter = Counter()
    started = time.monotonic()
    with results.open("a", encoding="utf-8") as sink:
        async with CountingClient(settings.http) as http:
            sweep = Sweep(http)

            async def worker(platform: str, queue: list[dict[str, Any]]) -> None:
                while queue and http.limited[platform] < STOP_AFTER_429:
                    row = await sweep.read(queue.pop())
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                    sink.flush()
                    counts[(platform, row["ok"])] += 1

            async def progress() -> None:
                while True:
                    await asyncio.sleep(60)
                    left = {p: len(q) for p, q in todo.items() if q}
                    print(f"{(time.monotonic() - started) / 60:.0f} min: {http.stats.requests} requests; "
                          f"ok {sum(n for (p, ok), n in counts.items() if ok)}, failed "
                          f"{sum(n for (p, ok), n in counts.items() if not ok)}; left {left}; 429s {dict(http.limited)}",
                          file=sys.stderr, flush=True)

            ticker = asyncio.create_task(progress())
            await asyncio.gather(*(worker(p, q) for p, q in todo.items() for _ in range(WORKERS.get(p, 2))))
            ticker.cancel()
            stopped = {p: len(q) for p, q in todo.items() if q}
            print(f"Done in {(time.monotonic() - started) / 60:.0f} min: {http.stats.summary()}", file=sys.stderr)
            if stopped:
                print(f"Stopped after repeated 429s, boards left: {stopped}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("boards", type=Path)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    asyncio.run(run(json.loads(args.boards.read_text(encoding="utf-8")), args.results))


if __name__ == "__main__":
    main()
