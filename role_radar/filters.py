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

Job descriptions are never downloaded, so they can't be matched on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Collection

from role_radar.models import JobPosting

# The fields a rule can be applied to. Add an entry here to make a new field matchable.
FIELD_GETTERS: dict[str, Callable[[JobPosting], str | None]] = {
    "title": lambda j: j.title,
    "location": lambda j: j.location,
    "employment_type": lambda j: j.employment_type,
    "department": lambda j: j.department,
}


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
            if name == "description":
                raise ValueError(
                    "match_on/exclude_on can't use 'description': Role Radar no longer downloads job "
                    "descriptions. Match on title (or location, employment_type, department) instead"
                )
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

    def fields_used(self) -> set[str]:
        """Job fields whose value can change this filter's verdict."""
        used = set(self.match_on) if self._include else set()
        if self._exclude:
            used.update(self.exclude_on)
        if self._locations:
            used.add("location")
        if self._types:
            used.add("employment_type")
        return used

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

    def could_match(self, job: JobPosting, unknown: Collection[str] = ()) -> bool:
        """Cheap pre-check before fetching a job's detail page.

        `unknown` names fields the listing doesn't really provide (empty fields
        count too), which the detail page may fill in. Returns False only when
        the job fails whatever those fields turn out to be, so the detail
        request can be skipped.
        """
        unknown = {*unknown, *(name for name, get in FIELD_GETTERS.items() if not get(job))}
        if self._search(self._exclude, job, [f for f in self.exclude_on if f not in unknown]):
            return False
        if self._locations and "location" not in unknown and not self._search(self._locations, job, ["location"]):
            return False
        if self._types and "employment_type" not in unknown and not self._search(self._types, job, ["employment_type"]):
            return False
        if self._include and not unknown.intersection(self.match_on):
            return bool(self._search(self._include, job, self.match_on))
        return True

    @staticmethod
    def _search(patterns: list[tuple[str, re.Pattern[str]]], job: JobPosting, fields: list[str]) -> str | None:
        texts = [t for t in (FIELD_GETTERS[f](job) for f in fields) if t]
        for keyword, pattern in patterns:
            if any(pattern.search(t) for t in texts):
                return keyword
        return None
