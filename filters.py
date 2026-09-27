"""Configurable job-matching rules.

A job matches when ALL of these hold:
  1. at least one include keyword appears in the `match_on` fields
     (or there are no include keywords),
  2. no exclude keyword appears in the `exclude_on` fields,
  3. its location matches one of `locations` (if any are configured),
  4. its employment type matches one of `employment_types` (if any are configured).

Keywords are case-insensitive and match on word boundaries, so "AI" matches
"AI Engineer" but not "Maintain". Spaces in a keyword also match hyphens,
slashes and underscores ("back end" matches "Back-End"). A keyword prefixed
with "re:" is used as a raw regular expression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from models import JobPosting

# The fields a rule can be applied to. Add an entry here to make a new field matchable.
FIELD_GETTERS: dict[str, Callable[[JobPosting], str | None]] = {
    "title": lambda j: j.title,
    "description": lambda j: j.description,
    "location": lambda j: j.location,
    "employment_type": lambda j: j.employment_type,
    "department": lambda j: j.department,
}

# Fields known without fetching a job's detail page.
LISTING_FIELDS = {"title", "location", "employment_type", "department"}


def compile_keyword(keyword: str) -> re.Pattern[str]:
    keyword = keyword.strip()
    if keyword.startswith("re:"):
        return re.compile(keyword[3:], re.I)
    words = [re.escape(w) for w in re.split(r"[\s\-_/]+", keyword) if w]
    body = r"[\s\-_/]*".join(words)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)


@dataclass
class MatchResult:
    matched: bool
    reason: str
    matched_keyword: str | None = None

    def __bool__(self) -> bool:
        return self.matched


@dataclass
class JobFilter:
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    match_on: list[str] = field(default_factory=lambda: ["title"])
    exclude_on: list[str] = field(default_factory=lambda: ["title"])
    locations: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in (*self.match_on, *self.exclude_on):
            if name not in FIELD_GETTERS:
                raise ValueError(f"Unknown filter field {name!r}; choose from {sorted(FIELD_GETTERS)}")
        self._include = [(k, compile_keyword(k)) for k in self.include_keywords]
        self._exclude = [(k, compile_keyword(k)) for k in self.exclude_keywords]
        self._locations = [(k, compile_keyword(k)) for k in self.locations]
        self._types = [(k, compile_keyword(k)) for k in self.employment_types]

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> JobFilter:
        cfg = cfg or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(cfg) - known
        if unknown:
            raise ValueError(f"Unknown filter options: {sorted(unknown)}")
        return cls(**{k: list(v) for k, v in cfg.items() if v is not None})

    @property
    def needs_description(self) -> bool:
        return "description" in self.match_on or "description" in self.exclude_on

    def evaluate(self, job: JobPosting) -> MatchResult:
        hit = self._search(self._exclude, job, self.exclude_on)
        if hit:
            return MatchResult(False, f"excluded by {hit!r}")
        if self._locations and not self._search(self._locations, job, ["location"]):
            return MatchResult(False, f"location {job.location!r} not in allowed locations")
        if self._types and not self._search(self._types, job, ["employment_type"]):
            return MatchResult(False, f"employment type {job.employment_type!r} not allowed")
        if not self._include:
            return MatchResult(True, "no include keywords configured")
        hit = self._search(self._include, job, self.match_on)
        if hit:
            return MatchResult(True, f"matched {hit!r}", hit)
        return MatchResult(False, "no include keyword matched")

    def could_match(self, job: JobPosting) -> bool:
        """Cheap pre-check using listing fields only, before fetching job details.

        Returns False only when the job is guaranteed to fail regardless of its
        description, so we can skip the detail request.
        """
        listing_exclude = [f for f in self.exclude_on if f in LISTING_FIELDS]
        if self._search(self._exclude, job, listing_exclude):
            return False
        if self._locations and job.location and not self._search(self._locations, job, ["location"]):
            return False
        if self._types and job.employment_type and not self._search(self._types, job, ["employment_type"]):
            return False
        if self._include and "description" not in self.match_on:
            return bool(self._search(self._include, job, self.match_on))
        return True

    @staticmethod
    def _search(patterns: list[tuple[str, re.Pattern[str]]], job: JobPosting, fields: list[str]) -> str | None:
        texts = [t for t in (FIELD_GETTERS[f](job) for f in fields) if t]
        for keyword, pattern in patterns:
            if any(pattern.search(t) for t in texts):
                return keyword
        return None
