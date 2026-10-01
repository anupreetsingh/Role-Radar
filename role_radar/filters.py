"""Configurable job-matching rules.

A job matches when ALL of these hold:
  1. at least one include keyword appears in the `match_on` fields
     (or there are no include keywords),
  2. no exclude keyword appears in the `exclude_on` fields, except as part of an include
     keyword found there: with "manager" excluded, "Product Manager" still matches the
     include keyword "product manager", but "Software Engineering Manager" is excluded,
  3. its location matches one of `locations` (if any are configured),
  4. its employment type matches one of `employment_types` (if any are configured).

Keywords are case-insensitive and match on word boundaries, so "AI" matches
"AI Engineer" but not "Maintain". Spaces in a keyword also match hyphens,
slashes and underscores ("back end" matches "Back-End"). A keyword prefixed
with "re:" is used as a raw regular expression.

Keywords never look at job descriptions. `max_experience_years` does, separately:
a new match whose description asks for more years is recorded but not alerted, and
`education` does the same for a higher degree than the person has (experience.py;
monitor.py reads each new match's description once).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Collection

from role_radar.models import JobPosting

# Highest education, lowest first: no degree, a bachelor's, a master's, a PhD.
EDUCATION = ("none", "bachelors", "masters", "phd")

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
    # Most years of experience a new match's description may ask for (a master's
    # counts where the posting says it does). None: descriptions aren't read for years.
    max_experience_years: int | None = None
    # The person's highest education (EDUCATION): a new match whose description requires
    # a higher degree isn't alerted, and "or a master's" paths count only for a master's or PhD.
    # None: a master's is assumed, and degrees aren't checked.
    education: str | None = None

    def __post_init__(self) -> None:
        for name in (*self.match_on, *self.exclude_on):
            if name == "description":
                raise ValueError(
                    "match_on/exclude_on can't use 'description': keywords don't match job descriptions. "
                    "Match on title (or location, employment_type, department) instead"
                )
            if name not in FIELD_GETTERS:
                raise ValueError(f"Unknown filter field {name!r}; choose from {sorted(FIELD_GETTERS)}")
        self._include = [(k, compile_keyword(k)) for k in self.include_keywords]
        self._exclude = [(k, compile_keyword(k)) for k in self.exclude_keywords]
        self._locations = [(k, compile_keyword(k)) for k in self.locations]
        self._types = [(k, compile_keyword(k)) for k in self.employment_types]
        years = self.max_experience_years
        if years is not None and (not isinstance(years, int) or isinstance(years, bool) or years < 0):
            raise ValueError("max_experience_years must be a whole number >= 0")
        if self.education is not None and self.education not in EDUCATION:
            raise ValueError(f"education must be one of {', '.join(EDUCATION)}, not {self.education!r}")

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> JobFilter:
        cfg = cfg or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(cfg) - known
        if unknown:
            raise ValueError(f"Unknown filter options: {sorted(unknown)}")
        scalars = ("max_experience_years", "education")
        return cls(**{k: v if k in scalars else list(v) for k, v in cfg.items() if v is not None})

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
        hit = self._excluded(job, self.exclude_on)
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
        if self._excluded(job, [f for f in self.exclude_on if f not in unknown]):
            return False
        if self._locations and "location" not in unknown and not self._search(self._locations, job, ["location"]):
            return False
        if self._types and "employment_type" not in unknown and not self._search(self._types, job, ["employment_type"]):
            return False
        if self._include and not unknown.intersection(self.match_on):
            return bool(self._search(self._include, job, self.match_on))
        return True

    def _excluded(self, job: JobPosting, fields: list[str]) -> str | None:
        """The first exclude keyword in `fields`, not counting where it's part of an include keyword
        found in the same field: the person asked for that title, word and all."""
        texts = [(text, [m.span() for _, p in self._include for m in p.finditer(text)] if name in self.match_on else [])
                 for name in fields if (text := FIELD_GETTERS[name](job))]
        for keyword, pattern in self._exclude:
            for text, asked in texts:
                if any(not any(start <= m.start() and m.end() <= end for start, end in asked) for m in pattern.finditer(text)):
                    return keyword
        return None

    @staticmethod
    def _search(patterns: list[tuple[str, re.Pattern[str]]], job: JobPosting, fields: list[str]) -> str | None:
        texts = [t for t in (FIELD_GETTERS[f](job) for f in fields) if t]
        for keyword, pattern in patterns:
            if any(pattern.search(t) for t in texts):
                return keyword
        return None
