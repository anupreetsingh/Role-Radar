import asyncio
import time

import httpx
import pytest

from role_radar.http_client import FetchError, HttpClient, HttpSettings, RobotsDisallowed, format_bytes
from tests.conftest import make_client


def test_host_wait_does_not_hold_a_global_slot():
    # One global slot. The second a.example request must wait 0.4s for a.example's
    # spacing, but b.example shouldn't have to wait behind it.
    done = {}

    def handler(request):
        return httpx.Response(200, text="ok")

    async def fetch(http, name, url, t0):
        await http.get_text(url)
        done[name] = time.monotonic() - t0

    async def go():
        async with make_client(handler, max_concurrency=1, host_delays={"a.example": 0.4}) as http:
            t0 = time.monotonic()
            await asyncio.gather(
                fetch(http, "a1", "https://a.example/1", t0),
                fetch(http, "a2", "https://a.example/2", t0),
                fetch(http, "b1", "https://b.example/1", t0),
            )

    asyncio.run(go())
    assert done["b1"] < 0.2  # didn't queue behind a2's wait
    assert done["a2"] >= 0.35  # a.example spacing still honoured


def test_host_spacing_is_measured_from_request_start():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(200, text="ok")

    async def go():
        async with make_client(handler, host_delays={"a.example": 0.15}) as http:
            await asyncio.gather(*(http.get_text(f"https://a.example/{i}") for i in range(3)))

    asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) == 2 and min(gaps) >= 0.14


def test_host_delay_overrides():
    s = HttpSettings(per_domain_delay=1.0, host_delays={"Example.com": 0.5, "api.example.com": 0})
    assert s.delay_for("example.com") == 0.5
    assert s.delay_for("jobs.example.com") == 0.5  # parent domain applies to subdomains
    assert s.delay_for("api.example.com") == 0  # most specific wins
    assert s.delay_for("other.org") == 1.0
    assert s.delay_for("boards-api.greenhouse.io") == 0.25  # built-in default
    assert HttpSettings(host_delays={"boards-api.greenhouse.io": 2}).delay_for("boards-api.greenhouse.io") == 2
    with pytest.raises(ValueError):
        HttpSettings(host_delays={"a.example": -1})


def test_stats_count_requests_bytes_and_errors():
    def handler(request):
        if request.url.host == "down.example":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 5000)

    async def go(http):
        await http.get_text("https://a.example/big")
        for url in ("https://a.example/missing", "https://down.example/"):
            with pytest.raises(FetchError):
                await http.get_text(url)

    http = make_client(handler, max_retries=0)
    asyncio.run(go(http))
    a, down = http.stats.hosts["a.example"], http.stats.hosts["down.example"]
    assert (a.requests, a.bytes, a.errors) == (2, 5000, 1)
    assert (down.requests, down.errors) == (1, 1)
    assert http.stats.requests == 3
    assert http.stats.summary().startswith("3 requests to 2 hosts, 4.9 KB downloaded, 2 failed; by host: a.example 2 req")
    assert [format_bytes(n) for n in (512, 2048, 3 * 1024**2)] == ["512 B", "2.0 KB", "3.0 MB"]


def test_retries_transient_errors_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    async def go():
        async with make_client(handler) as http:
            return await http.get_json("https://a.example/x")

    assert asyncio.run(go()) == {"ok": True} and len(calls) == 3


def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    async def go():
        async with make_client(handler) as http:
            await http.get_json("https://a.example/x")

    with pytest.raises(FetchError):
        asyncio.run(go())
    assert len(calls) == 1


def test_respects_robots_txt():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="ok")

    async def go():
        settings = HttpSettings(per_domain_delay=0, max_retries=0)
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            assert await http.get_text("https://a.example/public") == "ok"
            await http.get_text("https://a.example/private/page")

    with pytest.raises(RobotsDisallowed):
        asyncio.run(go())


@pytest.mark.parametrize("status, allowed", [(404, True), (401, True), (403, True), (503, False)])
def test_robots_status_handling_follows_rfc9309(status, allowed):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        return httpx.Response(200, text="ok")

    async def go():
        settings = HttpSettings(per_domain_delay=0, max_retries=0)
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            return await http.get_text("https://a.example/jobs")

    if allowed:
        assert asyncio.run(go()) == "ok"
    else:
        with pytest.raises(RobotsDisallowed):
            asyncio.run(go())
