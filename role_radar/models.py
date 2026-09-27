"""Core data model: a normalized job posting plus the identity rules for it."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that vary per visit/referrer and must not affect job identity.
_TRACKING_PARAMS = re.compile(r"^(utm_.*|gh_src|gh_jid_src|source|src|ref|lever-source.*|lever-origin|trk)$", re.I)
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)\b[^>]*>", re.I)


def normalize_text(value: str | None) -> str:
    """Lowercase, collapse whitespace, strip. Used for identity and matching."""
    if not value:
        return ""
    return _WS.sub(" ", value).strip().lower()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")


def normalize_url(url: str | None) -> str:
    """Canonicalize a URL so the same posting always yields the same string."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def html_to_text(value: str | None) -> str:
    """Cheap HTML → plain text, good enough for titles scraped from markup."""
    if not value:
        return ""
    text = html.unescape(value)
    # Some ATSs (Greenhouse) double-escape their HTML.
    if "&lt;" in text or "&gt;" in text:
        text = html.unescape(text)
    text = _BLOCK_TAG.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    lines = (_WS.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


@dataclass
class JobPosting:
    company: str
    title: str
    url: str
    source: str  # scraper that produced it, e.g. "bamboohr"
    job_id: str | None = None
    location: str | None = None
    employment_type: str | None = None
    department: str | None = None
    date_posted: date | None = None
    # Job descriptions aren't modelled or stored: only the experience filter reads one,
    # once, through the scraper's fetch_description().
    # Scraper-specific data needed to fetch details later (never persisted).
    extra: dict = field(default_factory=dict, repr=False, compare=False)
    _uid: str | None = field(default=None, init=False, repr=False, compare=False)
    _fingerprint: str | None = field(default=None, init=False, repr=False, compare=False)

    def freeze_identity(self) -> JobPosting:
        """Pin uid/fingerprint to the listing data.

        Detail pages can fill in fields (e.g. location) that feed the hashes;
        freezing first keeps a job's identity identical across runs whether or
        not its details were fetched.
        """
        self._uid, self._fingerprint = self.uid, self.fingerprint
        return self

    @property
    def uid(self) -> str:
        """Stable unique identifier.

        Prefer the ATS job ID (scoped by company + source so IDs from different
        boards can't collide); otherwise hash the normalized company, title,
        location and URL.
        """
        if self._uid:
            return self._uid
        company = slugify(self.company)
        if self.job_id:
            return f"{company}:{self.source}:{str(self.job_id).strip()}"
        raw = "|".join(
            (normalize_text(self.company), normalize_text(self.title), normalize_text(self.location), normalize_url(self.url))
        )
        return f"{company}:h:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    @property
    def fingerprint(self) -> str:
        """Content identity ignoring IDs/URLs, used to spot reposts and duplicates.

        Location is included on purpose: the same title in two cities is two jobs.
        """
        if self._fingerprint:
            return self._fingerprint
        raw = "|".join((normalize_text(self.company), normalize_text(self.title), normalize_text(self.location)))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
