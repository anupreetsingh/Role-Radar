"""robots.txt matching per RFC 9309, a drop-in for urllib.robotparser.RobotFileParser.

urllib's parser applies the first matching rule and ignores `*` and `$`, so it
gets real files wrong both ways: "Disallow: /" followed by "Allow: /api/jobs"
(Eightfold career sites) blocks the allowed API, and "Disallow: /results?*&page="
(Google Careers) doesn't block paging. RFC 9309 says the most specific (longest)
matching rule wins, an Allow wins a tie, `*` matches any run of characters and
`$` anchors the end of the path.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


class Robots:
    def __init__(self) -> None:
        self.disallow_all = False
        # user-agent (lower case) → [(allow, pattern)]; "*" is the default group.
        self.groups: dict[str, list[tuple[bool, str]]] = {}

    def parse(self, lines: list[str]) -> None:
        agents: list[str] = []
        in_rules = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:  # a user-agent line after rules starts a new group
                    agents, in_rules = [], False
                agents.append(value.lower())
                for agent in agents:
                    self.groups.setdefault(agent, [])
            elif key in ("allow", "disallow") and agents:
                in_rules = True
                if value:  # an empty Disallow allows everything: no rule
                    for agent in agents:
                        self.groups[agent].append((key == "allow", value))

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        if path == "/robots.txt":
            return True
        rules = self._rules_for(user_agent)
        best: tuple[int, bool] | None = None  # (pattern length, allow)
        for allow, pattern in rules:
            if _matches(pattern, path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:  # longer wins; on a tie, allow (True) wins
                    best = candidate
        return best is None or best[1]

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        product = user_agent.split("/", 1)[0].strip().lower()
        for agent, rules in self.groups.items():
            if agent != "*" and agent == product:
                return rules
        return self.groups.get("*", [])


def _matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    body = unquote(pattern[:-1] if anchored else pattern)
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored else ""), path) is not None
