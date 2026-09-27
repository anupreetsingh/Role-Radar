"""Small helpers for server-rendered listing cards and their pagination."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlsplit

from role_radar.models import html_to_text
from role_radar.scrapers.base import ScraperError


def next_link(page: str, base_url: str) -> str | None:
    for attrs, body in re.findall(r"<a\b([^>]*)>(.*?)</a\s*>", page, re.I | re.S):
        if re.search(r"\b(?:invisible|disabled)\b", attrs, re.I):
            continue
        text = html_to_text(body)
        if not re.match(r"^next(?:\s|[>»]|$)", text, re.I):
            continue
        match = re.search(r'''\bhref\s*=\s*["']([^"']+)["']''', attrs, re.I)
        if match:
            url = urljoin(base_url, unescape(match[1]))
            if urlsplit(url).netloc != urlsplit(base_url).netloc:
                raise ScraperError("listing pagination points to a different host")
            return url
    return None


def cards(page: str, tag: str, class_name: str) -> list[str]:
    return re.findall(
        rf'''<{tag}\b[^>]*class=["'][^"']*\b{re.escape(class_name)}\b[^"']*["'][^>]*>(.*?)</{tag}\s*>''',
        page, re.I | re.S,
    )
