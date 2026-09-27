import asyncio

import httpx
import pytest

from http_client import FetchError, HttpClient, HttpSettings, RobotsDisallowed
from tests.conftest import make_client


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
