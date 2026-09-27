"""Polite async HTTP client shared by all scrapers.

Every request goes through the same path:
  robots.txt check → per-host throttle → global concurrency slot → request
  → retry with exponential backoff on transient failures.

The per-host wait happens before a global slot is taken, so requests queued
behind one busy shared host (boards-api.greenhouse.io serves every Greenhouse
board) never hold slots that requests to other hosts could use.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "RoleRadar/1.0 (personal job-alert monitor; +https://github.com/)"
RETRY_STATUSES = {429, 500, 502, 503, 504}

# The big ATS APIs serve every company's board from one host, so they get a
# shorter delay than per_domain_delay. settings.http.host_delays adds to and
# overrides these.
DEFAULT_HOST_DELAYS = {
    "boards-api.greenhouse.io": 0.25,
    "api.lever.co": 0.3,
    "api.eu.lever.co": 0.3,
    "api.ashbyhq.com": 0.3,
}


class FetchError(Exception):
    """A request failed permanently (after retries, or for a non-retryable reason)."""


class RobotsDisallowed(FetchError):
    pass


@dataclass
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    max_concurrency: int = 16
    per_domain_delay: float = 1.0  # minimum seconds between requests to one host
    # Per-host overrides of per_domain_delay, e.g. {"boards-api.greenhouse.io": 0.25}.
    # A key also covers its subdomains; the most specific key wins.
    host_delays: dict[str, float] = field(default_factory=dict)
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.5
    max_backoff: float = 60.0
    respect_robots: bool = True

    def __post_init__(self) -> None:
        delays = {}
        for host, delay in (self.host_delays or {}).items():
            if not isinstance(delay, (int, float)) or delay < 0:
                raise ValueError(f"host_delays[{host!r}] must be a number of seconds >= 0")
            delays[str(host).strip().lower()] = float(delay)
        self.host_delays = delays

    def delay_for(self, host: str) -> float:
        """Minimum seconds between requests to `host`."""
        host = host.lower()
        delays = {**DEFAULT_HOST_DELAYS, **self.host_delays}
        matches = [name for name in delays if host == name or host.endswith("." + name)]
        return delays[max(matches, key=len)] if matches else self.per_domain_delay


@dataclass
class HostStats:
    requests: int = 0
    bytes: int = 0  # as downloaded, i.e. compressed
    errors: int = 0  # transport errors and HTTP status >= 400


class HttpStats:
    """Requests, bytes and errors per host, for the end-of-run log."""

    def __init__(self) -> None:
        self.hosts: dict[str, HostStats] = {}

    def record(self, host: str, nbytes: int = 0, error: bool = False) -> None:
        stats = self.hosts.setdefault(host, HostStats())
        stats.requests += 1
        stats.bytes += nbytes
        stats.errors += error

    @property
    def requests(self) -> int:
        return sum(s.requests for s in self.hosts.values())

    @property
    def bytes(self) -> int:
        return sum(s.bytes for s in self.hosts.values())

    def busiest(self) -> list[tuple[str, HostStats]]:
        return sorted(self.hosts.items(), key=lambda kv: (-kv[1].requests, kv[0]))

    def summary(self, top: int = 10) -> str:
        """One line: totals, then the hosts with the most requests."""
        busiest = self.busiest()
        hosts = ", ".join(f"{h} {s.requests} req/{format_bytes(s.bytes)}" for h, s in busiest[:top])
        more = f" (+{len(busiest) - top} more hosts)" if len(busiest) > top else ""
        errors = sum(s.errors for s in self.hosts.values())
        return (
            f"{self.requests} requests to {len(busiest)} hosts, {format_bytes(self.bytes)} downloaded, "
            f"{errors} failed" + (f"; by host: {hosts}{more}" if hosts else "")
        )


def format_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024**2:.1f} MB"


class HttpClient:
    def __init__(self, settings: HttpSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = s = settings or HttpSettings()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": s.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=httpx.Timeout(s.read_timeout, connect=s.connect_timeout),
            follow_redirects=True,
            transport=transport,
        )
        self.stats = HttpStats()
        self._global = asyncio.Semaphore(s.max_concurrency)
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # -- public helpers -----------------------------------------------------

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._parse_json(await self.request("GET", url, **kwargs), url)

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return self._parse_json(await self.request("POST", url, json=payload, **kwargs), url)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text

    # -- core ---------------------------------------------------------------

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.settings.respect_robots and not await self._allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        attempt = 0
        while True:
            try:
                resp = await self._send(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.settings.max_retries:
                    raise FetchError(f"{method} {url} failed: {exc!r}") from exc
                delay = self._backoff(attempt)
                log.warning("%s %s: %s; retrying in %.1fs", method, url, type(exc).__name__, delay)
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRY_STATUSES or attempt >= self.settings.max_retries:
                    raise FetchError(f"{method} {url} returned HTTP {resp.status_code}")
                delay = self._retry_after(resp) or self._backoff(attempt)
                log.warning("%s %s: HTTP %s; retrying in %.1fs", method, url, resp.status_code, delay)
            attempt += 1
            await asyncio.sleep(delay)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        await self._wait_turn(host)
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError:
            self.stats.record(host, error=True)
            raise
        finally:
            self._global.release()
        # Bytes as received (compressed). Responses built in memory (tests) report 0 there.
        nbytes = resp.num_bytes_downloaded or len(resp.content)
        self.stats.record(host, nbytes, error=resp.status_code >= 400)
        return resp

    async def _wait_turn(self, host: str) -> None:
        """Wait out the host's spacing, then take a global slot (returned held).

        The host lock is kept until the slot is taken, so the spacing is
        measured from when requests actually start, not from when they
        finished waiting on the host.
        """
        lock = self._domain_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._domain_last.get(host, 0.0) + self.settings.delay_for(host) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._global.acquire()
            self._domain_last[host] = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        delay = self.settings.backoff_base * (2**attempt)
        return min(self.settings.max_backoff, delay) * random.uniform(0.8, 1.2)

    def _retry_after(self, resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        if value and value.strip().isdigit():
            return min(float(value), self.settings.max_backoff)
        return None

    @staticmethod
    def _parse_json(resp: httpx.Response, url: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"{url} did not return valid JSON") from exc

    # -- robots.txt ---------------------------------------------------------

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self.settings.user_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        """Fetch robots.txt, following RFC 9309 status handling.

        4xx ("unavailable")        → no restrictions
        5xx / network ("unreachable") → disallow everything for this run
        """
        try:
            resp = await self._send("GET", f"{origin}/robots.txt")
        except httpx.HTTPError:
            resp = None
        if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
            return None
        parser = RobotFileParser()
        if resp is None or resp.status_code >= 400:
            log.warning("robots.txt for %s unreachable; skipping this host for this run", origin)
            parser.disallow_all = True
            return parser
        parser.parse(resp.text.splitlines())
        return parser
