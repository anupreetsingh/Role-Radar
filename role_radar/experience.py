"""Experience filter: does a job's description ask for more experience than allowed?

Only new title matches are checked, once each (monitor.py). The description is read:
  text → lines, grouped under their section headings (preferred / nice-to-have and
  about-us / benefits sections are skipped) → sentences → the sentence's alternative
  paths ("..., or a Master's degree with 2+ years") → the years each path needs.
A sentence needs the fewest years of its paths (any one will do); a path naming a
master's degree and no years needs none, and a PhD-only path doesn't count. The job
needs the most years of its sentences (all of them apply).

Anything unclear keeps the job: a good job dropped by mistake is worse than an
extra alert. So a description that can't be read, has no years, or says a master's
may substitute for experience is kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from role_radar.models import html_to_text

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}  # fmt: skip
_NUM = r"(?:\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
MAX_PLAUSIBLE = 12  # more than this, for a non-senior title, is about the company, not the candidate

# "3+ years", "at least three (3) years", "2-4 yrs", "5 or more years", "1 year(s)"
_YEARS = re.compile(
    rf"""(?:(?P<bound>no\s+less\s+than|not\s+less\s+than|at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|min\.?
          |more\s+than|greater\s+than|in\s+excess\s+of|over
          |up\s+to|less\s+than|fewer\s+than|under|no\s+more\s+than|a\s+maximum\s+of|maximum\s+of)\s+)?
        (?<![\w.])(?P<n>{_NUM})(?:\s*\(\s*\d{{1,2}}\s*\))?\s*(?:\+|plus)?\s*
        (?:(?:-|–|—|to)\s*(?P<m>{_NUM})\s*\+?\s*)?
        (?:or\s+more\s+|or\s+greater\s+|and\s+above\s+)?
        (?:full[\s-]time\s+)?
        (?P<unit>years?|yrs?|yoe)(?:\s*\(s\))?(?![\w-])""",
    re.I | re.X,
)
# "Years of experience: 3+"
_YEARS_FIRST = re.compile(rf"\byears\s+of\s+(?:\w+\s+){{0,2}}experience\s*(?:required)?\s*[:\-–]\s*(?P<n>{_NUM})\b", re.I)
_UPPER_BOUND = re.compile(r"up\s+to|less\s+than|fewer\s+than|under|no\s+more\s+than|maximum", re.I)
# Text right before or after a number of years that makes it not a requirement.
_NOT_BEFORE = re.compile(
    r"\b(?:within|in|every|after|past|last|next|first|each|per|since|consecutive)\s+"
    r"(?:(?:the|a|an|over|about|nearly|almost|approximately|roughly)\s+){0,2}$",
    re.I,
)
_NOT_AFTER = re.compile(
    r"^'?\s*(?:old|ago|of\s+age|in\s+a\s+row|running|straight|consecutive|degree|program|programme|college|university"
    r"|warranty|term|contract|anniversary|plan|vesting|cliff"
    r"|of\s+(?:college|university|school|study|studies|coursework|undergraduate|academic|tuition|service|employment))\b",
    re.I,
)
# Before a number of years that opens its sentence: "8+ years technical program management".
_LEADS_SENTENCE = re.compile(r"^\W*(?:(?:you\s+)?(?:have|bring|possess|need)\s+|with\s+)?$", re.I)
_EXPERIENCE_WORD = re.compile(
    r"\b(?:experiences?|experienced|exp|yoe|background|track\s+record|professional(?:ly)?|industry|hands[\s-]on)\b", re.I
)
_CONNECTOR_AFTER = re.compile(
    r"^'?\s*(?:of|in|with|building|developing|designing|writing|working|shipping|leading|doing|programming|coding"
    r"|using|as|at|post|combined|total|relevant|related|hands)\b",
    re.I,
)

_MASTERS = (
    r"(?:\b(?<!scrum\s)master'?s?\b(?!\s+data)|\bmasters\b|(?-i:\bMS\b(?!\s+(?:Office|Excel|Word|SQL|Teams|Azure|Windows"
    r"|Dynamics|Project|Access|Visio|Power|Outlook|365))|\bM\.\s?S\b\.?|\bMSc\b|\bM\.Sc\b\.?|\bMEng\b|\bM\.Eng\b\.?"
    r"|\bM\.?Tech\b)|\b(?:graduate|advanced|post-?graduate)\s+degree)"
)
_PHD = r"(?:\bph\.?\s?d\b\.?|\bdoctorate\b|\bdoctoral\b)"
_BACHELORS = (
    r"(?:\bbachelor'?s?\b|(?-i:\bBS\b|\bB\.\s?S\b\.?|\bBA\b|\bB\.\s?A\b\.?|\bBSc\b|\bBEng\b|\bB\.?Tech\b)"
    r"|\bundergraduate\s+degree\b)"
)
_DEGREE = rf"(?:{_MASTERS}|{_PHD}|{_BACHELORS}|\bdegree\b)"
MASTERS = re.compile(_MASTERS, re.I)
# "(3+ with a Master's)": the word "years" left out before a master's
_YEARS_WITH_MASTERS = re.compile(rf"(?<![\w.])(?P<n>{_NUM})\s*\+?\s*(?=(?:with|w/)\s+(?:an?\s+)?{_MASTERS})", re.I)
PHD = re.compile(_PHD, re.I)
BACHELORS = re.compile(_BACHELORS, re.I)

# Where a sentence's alternative paths split: " or " / "(" before a degree or a number,
# and every ")". "Python or Go" and "or equivalent experience" don't split.
_LEAD = r"(?:(?:or|an?|the|with|equivalent|relevant)\s+)*"
_PATH_SPLIT = re.compile(rf"\s+or\s+(?={_LEAD}(?:{_DEGREE}|{_NUM}\b))|\((?=\s*{_LEAD}(?:{_DEGREE}|{_NUM}\b))|\)", re.I)
# A number of years attached to a master's that follows it: "2+ years with an MS".
_ATTACHED_BEFORE = re.compile(
    r"^\s*(?:of\s+[^,;]{0,40}?)?(?:experience\s+)?(?:with|w/|and|\+|plus)\s+(?:an?\s+)?(?:relevant\s+)?$", re.I
)

_PREFERRED = re.compile(
    r"\b(?:preferred|preferably|nice[\s-]to[\s-]haves?|an?\s+(?:big\s+|huge\s+|strong\s+|definite\s+)?(?:plus|advantage)"
    r"|pluses|bonus|ideally|desired|desirable|advantageous|not\s+required|optional|extra\s+credit|stand\s+out)\b",
    re.I,
)
_SUBSTITUTE = re.compile(
    rf"{_MASTERS}[^.;]{{0,100}}?(?:in\s+lieu|substitut|counts?\s+(?:as|toward)|equivalent\s+to|instead\s+of"
    rf"|in\s+place\s+of|(?:may|can)\s+replace)|(?:in\s+lieu|substitut)[^.;]{{0,100}}?{_MASTERS}",
    re.I,
)
# A path's closing words that make it a preference: "3+ years of experience is ideal".
_PREFERRED_END = re.compile(
    r"(?:preferred|a\s+(?:big\s+|huge\s+|strong\s+|definite\s+)?(?:plus|advantage)|ideal|desired|desirable|advantageous"
    r"|nice[\s-]to[\s-]have|bonus|optional|not\s+required)\W*$",
    re.I,
)
# A sentence about the company rather than the candidate.
_COMPANY = re.compile(r"\b(?:we|we're|we've|our|us|founded)\b", re.I)
_CANDIDATE = re.compile(
    r"\b(?:you|your|you'll|you've|you're|candidates?|applicants?|required|requires?|requirements?|must|minimum|at\s+least"
    r"|looking\s+for|seeking|need|qualif\w*)\b",
    re.I,
)

# Section headings. A heading is a short line; what it names decides whether the
# lines under it count.
_HEADING_START = re.compile(
    r"^(?:preferred|nice|bonus|minimum|basic|required|requirements?|qualifications?|about|what|who|why|benefits|perks"
    r"|responsibilities|the\s+role|your\s+role|you|skills|must|desired|additional|compensation|pay|salary|our|job)\b",
    re.I,
)
_ABOUT = re.compile(
    r"^(?:about(?!\s+(?:you|the\s+role|this\s+role|the\s+job|the\s+position|the\s+opportunity))\b|who\s+we\s+are"
    r"|why\s+(?:join|work)|our\s+(?:company|mission|story|team|culture|values)|benefits|perks|compensation|pay\b|salary"
    r"|what\s+we\s+offer|equal\s+(?:employment|opportunity))",
    re.I,
)
_BULLET = re.compile(r"^[\s\-–—•*·●▪◦]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])|;\s*|\s+[•·●▪]\s+")
_ABBREVIATION = re.compile(r"(?:\b(?:[A-Za-z]\.){1,3}|\b(?:Ph\.D|e\.g|i\.e|etc|incl|approx|vs|yrs|No|Sr|Jr|St|Inc|Ltd|Co))\.?$")


@dataclass
class ExperienceVerdict:
    keep: bool
    reason: str
    years: int | None = None  # the most years a sentence required (None: no requirement found)


def assess(description: str | None, max_years: int) -> ExperienceVerdict:
    """Whether a job whose description is `description` (HTML or text) fits `max_years` of experience."""
    if not description or not description.strip():
        return ExperienceVerdict(True, "no description to read")
    text = html_to_text(description).replace("’", "'").replace("‘", "'")
    worst: tuple[int, str] | None = None
    for sentence in _required_sentences(text):
        years = _sentence_years(sentence)
        if years is not None and (worst is None or years > worst[0]):
            worst = (years, sentence)
    if worst is None:
        return ExperienceVerdict(True, "no years of experience required")
    years, sentence = worst
    if years <= max_years:
        return ExperienceVerdict(True, f"needs {years} year{'' if years == 1 else 's'}", years)
    if _SUBSTITUTE.search(text):
        return ExperienceVerdict(True, f"needs {years}+ years, but a master's may substitute", years)
    quote = sentence if len(sentence) <= 160 else sentence[:157] + "..."
    return ExperienceVerdict(False, f'needs {years}+ years: "{quote}"', years)


def _required_sentences(text: str) -> list[str]:
    """The description's sentences outside preferred and about-the-company sections."""
    sentences = []
    skipping = False
    for raw in text.splitlines():
        line = _BULLET.sub("", raw).strip()
        if not line:
            continue
        if _is_heading(line):
            skipping = bool(_PREFERRED.search(line) or _ABOUT.match(line))
            continue
        if not skipping:
            sentences += _split_sentences(line)
    return sentences


def _is_heading(line: str) -> bool:
    words = line.rstrip(":").split()
    if not words or len(line) > 70 or len(words) > 8 or line.endswith((".", ",", ";")) or _YEARS.search(line):
        return False
    if line.endswith(":"):
        return True
    return bool(_HEADING_START.match(line)) and len(words) <= 6


def _split_sentences(line: str) -> list[str]:
    pieces: list[str] = []
    for piece in _SENTENCE_END.split(line):
        if pieces and _ABBREVIATION.search(pieces[-1]):
            pieces[-1] += " " + piece
        elif piece.strip():
            pieces.append(piece.strip())
    return pieces


def _sentence_years(sentence: str) -> int | None:
    """Years the sentence requires: the fewest over its paths, or None if it requires none."""
    mentions = _mentions(sentence)
    if not mentions:
        return None
    if _COMPANY.search(sentence) and not _CANDIDATE.search(sentence):
        return None
    has_context = bool(_EXPERIENCE_WORD.search(sentence))
    counted = [m for m in mentions if has_context or m.connected]
    if not counted:
        return None
    paths = _paths(sentence, counted)
    needs = []
    for i, path in enumerate(paths):
        if path.preferred:
            continue
        if path.masters and path.years:
            needs.append(path.masters_years)
        elif path.masters:
            # A master's with no years of its own is a path of its own ("3+ years, or a Master's";
            # "a Master's, or a Bachelor's with 4+ years"), unless it heads a list of degrees that
            # share the next one's years ("a Master's or PhD in CS, and 4+ years").
            following = paths[i + 1] if i + 1 < len(paths) else None
            shared = following and following.years and not following.bachelors and not any(p.years for p in paths[:i])
            needs.append(max(following.years) if shared else 0)
        elif path.years and not path.phd_only:
            needs.append(max(path.years))
    return min(needs) if needs else None


@dataclass
class _Path:
    text: str
    years: list[int]  # the years each counted mention in this path asks for
    masters: bool
    masters_years: int  # with a master's: the years tied to it
    bachelors: bool
    phd_only: bool
    preferred: bool


def _paths(sentence: str, mentions: list[_Mention]) -> list[_Path]:
    """The sentence split into its alternative paths: at " or " / "(" before a degree or a number, and at ")"."""
    bounds, start = [], 0
    for m in _PATH_SPLIT.finditer(sentence):
        bounds.append((start, m.start()))
        start = m.end()
    bounds.append((start, len(sentence)))
    paths = []
    for lo, hi in bounds:
        text = sentence[lo:hi]
        inside = [m for m in mentions if lo <= m.start < hi]
        masters = MASTERS.search(text)
        masters_years = 0
        if masters:
            after = [m.years for m in inside if m.start - lo > masters.start()]
            attached = [m.years for m in inside if _ATTACHED_BEFORE.match(text[m.end - lo : masters.start()])]
            masters_years = max(after or attached or [m.years for m in inside] or [0])
        # A preference word before the years ("ideally 4+ years") or closing the path ("... is a plus")
        # makes it optional; after them it's about the kind of experience ("5+ years, ideally in fintech").
        opening = text[: inside[0].start - lo] if inside else text
        paths.append(_Path(
            text=text, years=[m.years for m in inside], masters=bool(masters), masters_years=masters_years,
            bachelors=bool(BACHELORS.search(text)),
            preferred=bool(_PREFERRED.search(opening) or _PREFERRED_END.search(text)),
            phd_only=bool(PHD.search(text)) and not BACHELORS.search(text),
        ))  # fmt: skip
    return paths


@dataclass
class _Mention:
    start: int
    end: int
    years: int
    connected: bool  # followed by "of", "in", "building"... so it's about experience even without the word


def _mentions(sentence: str) -> list[_Mention]:
    found = []
    for m in _YEARS.finditer(sentence):
        before, after = sentence[: m.start()], sentence[m.end() :]
        if _NOT_BEFORE.search(before) or _NOT_AFTER.match(after):
            continue
        years = _number(m.group("n"))
        if m.group("bound") and _UPPER_BOUND.fullmatch(m.group("bound").strip()):
            years = 0
        if years > MAX_PLAUSIBLE:
            continue
        connected = bool(_CONNECTOR_AFTER.match(after) or _LEADS_SENTENCE.match(before))
        found.append(_Mention(m.start(), m.end(), years, connected))
    for m in (*_YEARS_FIRST.finditer(sentence), *_YEARS_WITH_MASTERS.finditer(sentence)):
        years = _number(m.group("n"))
        if years <= MAX_PLAUSIBLE:
            found.append(_Mention(m.start("n"), m.end("n"), years, True))
    return sorted(found, key=lambda m: m.start)


def _number(token: str) -> int:
    token = token.lower()
    return NUMBER_WORDS[token] if token in NUMBER_WORDS else int(token)
