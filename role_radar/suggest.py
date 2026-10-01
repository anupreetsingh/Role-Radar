"""Companies people ask to have on everyone's lists, for the maintainer to add in a new version.

The app's Companies page → `role-radar setup suggest` (JSON on stdin) → a line in suggestions.jsonl
beside the companies file → POSTed to the suggestions box (deploy/suggestions: a Lambda function URL
writing to a DynamoDB table) → `role-radar suggestions` lists them for the maintainer, the same company
once with how many asked. A suggestion that can't be sent stays in the file and goes with the next one.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from role_radar import __version__
from role_radar.config import profile_path
from role_radar.storage import to_iso, utcnow

# The suggestions box: deploy/suggestions' SuggestionsUrl output. $ROLE_RADAR_SUGGESTIONS_URL overrides it.
ENDPOINT = "https://jpgqzrwhtsaeplsmw35a5rp7oa0qohwq.lambda-url.us-east-1.on.aws/"
TABLE = "role-radar-suggestions"
LIMITS = {"name": 120, "url": 500, "note": 1000}


def endpoint() -> str | None:
    return os.environ.get("ROLE_RADAR_SUGGESTIONS_URL") or ENDPOINT or None


def waiting_file(config: Path) -> Path:
    return Path(config).with_name("suggestions.jsonl")


def suggest(config: Path, name: str, url: str = "", note: str = "") -> dict[str, Any]:
    """Send a company for everyone's lists (with any not sent before). {"sent", "waiting", "error"}."""
    name, url, note = name.strip(), url.strip(), note.strip()
    if not name:
        raise ValueError("give the company's name")
    if url and not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        raise ValueError("a careers page's address starts with https://")
    for field, value in (("name", name), ("url", url), ("note", note)):
        if len(value) > LIMITS[field]:
            raise ValueError(f"the {field} is too long (at most {LIMITS[field]} characters)")
    profile = profile_path(config)
    settings = ((yaml.safe_load(profile.read_text(encoding="utf-8")) or {}).get("settings") or {}) if profile.exists() else {}
    entry = {"id": uuid.uuid4().hex, "name": name, "url": url or None, "note": note or None,
             "profession": settings.get("profession"), "countries": list(settings.get("countries") or []),
             "app": __version__, "at": to_iso(utcnow())}
    with waiting_file(config).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return send_waiting(config)


def send_waiting(config: Path) -> dict[str, Any]:
    """POST every suggestion waiting in the file; those that don't go stay for next time."""
    path = waiting_file(config)
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.exists() else []
    url, left, sent, error = endpoint(), [], 0, None
    for entry in entries:
        if not url:
            left.append(entry)
            error = "the suggestions box isn't set up in this version"
            continue
        try:
            httpx.post(url, json=entry, timeout=15, headers={"User-Agent": f"RoleRadar/{__version__}"}).raise_for_status()
            sent += 1
        except httpx.HTTPError as exc:
            left.append(entry)
            error = f"couldn't reach the suggestions box ({type(exc).__name__}); it goes with the next one"
    if left:
        path.write_text("".join(json.dumps(e) + "\n" for e in left), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return {"sent": sent, "waiting": len(left), "error": error}


# -- the maintainer's side -------------------------------------------------------------------------


def _client(client: Any = None) -> Any:
    if client is not None:
        return client
    import boto3

    return boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                        or "us-east-1")


def list_suggestions(*, everything: bool = False, client: Any = None, table: str = TABLE) -> list[dict[str, Any]]:
    """The suggestions box, most asked for first (then newest): those not yet marked done, or every one."""
    db, rows, start = _client(client), [], None
    while True:
        page = db.scan(TableName=table, **({"ExclusiveStartKey": start} if start else {}))
        for item in page.get("Items", []):
            row = {key: _plain(value) for key, value in item.items()}
            if everything or row.get("status") != "done":
                rows.append(row)
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    rows.sort(key=lambda r: r.get("last_at") or "", reverse=True)
    rows.sort(key=lambda r: int(r.get("requests") or 0), reverse=True)
    return rows


def mark_done(ids: list[str], *, client: Any = None, table: str = TABLE) -> None:
    """Mark suggestions as dealt with (added in a version, or not), so the list shows what's left."""
    db = _client(client)
    for key in ids:
        db.update_item(TableName=table, Key={"id": {"S": key}}, UpdateExpression="SET #s = :done",
                       ConditionExpression="attribute_exists(id)",
                       ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":done": {"S": "done"}})


def _plain(value: dict[str, Any]) -> Any:
    """A DynamoDB attribute value as plain JSON."""
    kind, inner = next(iter(value.items()))
    if kind == "N":
        return int(inner) if inner.lstrip("-").isdigit() else float(inner)
    if kind in ("SS", "NS"):
        return sorted(inner)
    if kind == "L":
        return [_plain(v) for v in inner]
    if kind == "M":
        return {k: _plain(v) for k, v in inner.items()}
    return inner
