"""Polite async HTTP client shared by all scrapers.

Every request goes through the same path:
  robots.txt check → global concurrency slot → per-domain throttle → request
  → retry with exponential backoff on transient failures.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "RoleRadar/1.0 (personal job-alert monitor; +https://github.com/)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """A request failed permanently (after retries, or for a non-retryable reason)."""


class RobotsDisallowed(FetchError):
    pass


@dataclass
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    max_concurrency: int = 8
    per_domain_delay: float = 1.0  # minimum seconds between requests to one host
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.5
    max_backoff: float = 60.0
    respect_robots: bool = True


class HttpClient:
    def __init__(self, settings: HttpSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = s = settings or HttpSettings()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": s.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=httpx.Timeout(s.read_timeout, connect=s.connect_timeout),
            follow_redirects=True,
            transport=transport,
        )
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
        async with self._global:
            await self._throttle(urlsplit(url).netloc)
            return await self._client.request(method, url, **kwargs)

    async def _throttle(self, host: str) -> None:
        lock = self._domain_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._domain_last.get(host, 0.0) + self.settings.per_domain_delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
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
