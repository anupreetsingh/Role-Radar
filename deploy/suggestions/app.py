"""Role Radar's suggestions box: the function URL the app's Companies page posts to (role_radar/suggest.py).

Each POST is one suggestion, JSON: {"name", "url"?, "note"?, "profession"?, "countries"?, "app"?}.
The same company (by its careers page, else by name) is kept once, counting how many asked;
`role-radar suggestions` lists them. No suggestion is read back here: the box only takes them in.

It also serves the two badges on the project's GitHub page, in shields.io's endpoint format:
- GET /badge/downloads: how many times the app's disk image was downloaded from the Releases page,
  every version together. Updates fetch a copy named ...-update.dmg (scripts/publish_update.sh), so
  they don't count.
- GET /badge/users: how many copies of the app ran in the last 7 days. Each copy POSTs /checkin when
  it opens and every 6 hours: {"id": a random id made once on that Mac, "app", "os"}, one row per id
  in the installs table.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import boto3

TABLE = os.environ.get("TABLE", "role-radar-suggestions")
STATS = os.environ.get("STATS", "role-radar-stats")
INSTALLS = os.environ.get("INSTALLS", "role-radar-installs")
INSTALL_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
WEEK = 7 * 86400
REPO = os.environ.get("REPO", "anupreetsingh/Role-Radar")
DOWNLOAD = re.compile(r"^Role-Radar-[0-9.]+-apple-silicon\.dmg$")  # as people download it, not as updates do
REFRESH = 1800  # seconds between asking GitHub; the badge shows the count kept in between
LIMITS = {"name": 120, "url": 500, "note": 1000, "profession": 20, "app": 40}
MAX_BODY = 8000
_db = None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    method = (event.get("requestContext") or {}).get("http", {}).get("method")
    path = (event.get("rawPath") or "/").rstrip("/") or "/"
    if method == "GET" and path == "/badge/downloads":
        return _badge("downloads", downloads())
    if method == "GET" and path == "/badge/users":
        return _badge("weekly users", weekly_users())
    if method != "POST":
        return _reply(405, "POST a suggestion")
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        return _reply(413, "too long")
    try:
        data = json.loads(raw)
    except ValueError:
        return _reply(400, "send JSON")
    if not isinstance(data, dict):
        return _reply(400, "send a JSON object")
    if path == "/checkin":
        return check_in(data)
    fields = {k: str(data.get(k) or "").strip() for k in LIMITS}
    if not fields["name"]:
        return _reply(400, "give the company's name")
    if fields["url"] and not re.match(r"^https?://[^\s/]+\.[^\s/]+", fields["url"]):
        return _reply(400, "a careers page's address starts with https://")
    for key, value in fields.items():
        if len(value) > LIMITS[key]:
            return _reply(400, f"{key} too long")
    countries = sorted({c for c in data.get("countries") or [] if isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c)})[:4]
    save(fields, countries)
    return _reply(200, "thanks")


def key_for(name: str, url: str) -> str:
    """The same company is one row: its careers page (host and path, any case), else its name."""
    if url:
        parts = urlsplit(url)
        return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/').lower()}"
    return "name:" + re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def save(fields: dict[str, str], countries: list[str]) -> None:
    global _db
    _db = _db or boto3.client("dynamodb")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    names = {"#n": "name", "#s": "status"}
    values: dict[str, Any] = {":name": {"S": fields["name"]}, ":now": {"S": now}, ":new": {"S": "new"}, ":one": {"N": "1"}}
    sets = ["#n = if_not_exists(#n, :name)", "first_at = if_not_exists(first_at, :now)", "last_at = :now",
            "#s = if_not_exists(#s, :new)"]
    adds = ["requests :one"]
    if fields["url"]:
        names["#u"] = "url"
        values[":url"] = {"S": fields["url"]}
        sets.append("#u = if_not_exists(#u, :url)")
    if fields["note"]:
        values[":none"], values[":note"] = {"L": []}, {"L": [{"S": f"{now} {fields['note']}"}]}
        sets.append("notes = list_append(if_not_exists(notes, :none), :note)")
    if fields["app"]:
        values[":app"] = {"S": fields["app"]}
        sets.append("app = :app")
    if fields["profession"]:
        values[":p"] = {"SS": [fields["profession"]]}
        adds.append("professions :p")
    if countries:
        values[":c"] = {"SS": countries}
        adds.append("countries :c")
    _db.update_item(TableName=TABLE, Key={"id": {"S": key_for(fields["name"], fields["url"])}},
                    UpdateExpression="SET " + ", ".join(sets) + " ADD " + ", ".join(adds),
                    ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def check_in(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the app in use: its row says when it was first and last seen, on which versions."""
    install = str(data.get("id") or "")
    if not INSTALL_ID.match(install):
        return _reply(400, "send the app's id")
    global _db
    _db = _db or boto3.client("dynamodb")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _db.update_item(TableName=INSTALLS, Key={"id": {"S": install.lower()}},
                    UpdateExpression="SET first_seen = if_not_exists(first_seen, :now), last_seen = :now, app = :app, "
                                     "os = :os ADD checkins :one",
                    ExpressionAttributeValues={":now": {"S": now}, ":app": {"S": str(data.get("app") or "?")[:40]},
                                               ":os": {"S": str(data.get("os") or "?")[:40]}, ":one": {"N": "1"}})
    return _reply(200, "thanks")


def weekly_users() -> int:
    """Copies of the app that checked in over the last 7 days."""
    global _db
    _db = _db or boto3.client("dynamodb")
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - WEEK))
    count, start = 0, None
    while True:
        page = _db.scan(TableName=INSTALLS, ProjectionExpression="last_seen", **({"ExclusiveStartKey": start} if start else {}))
        count += sum(1 for item in page.get("Items", []) if item.get("last_seen", {}).get("S", "") >= since)
        start = page.get("LastEvaluatedKey")
        if not start:
            return count


def _badge(label: str, count: int | None) -> dict[str, Any]:
    """A shields.io endpoint badge, read fresh at most hourly."""
    body = {"schemaVersion": 1, "label": label, "message": "unknown" if count is None else f"{count:,}",
            "color": "blue", "cacheSeconds": 3600}
    return {"statusCode": 200, "headers": {"content-type": "application/json", "cache-control": "max-age=3600"},
            "body": json.dumps(body)}


def downloads() -> int | None:
    """Downloads of the app from the Releases page, every version together. GitHub is asked at most every
    REFRESH seconds; the count is kept in the stats table, so a slow or rate-limited GitHub shows the last one."""
    global _db
    _db = _db or boto3.client("dynamodb")
    row = _db.get_item(TableName=STATS, Key={"id": {"S": "downloads"}}).get("Item") or {}
    kept = int(row["count"]["N"]) if "count" in row else None
    if kept is not None and time.time() - float(row.get("at", {}).get("N", "0")) < REFRESH:
        return kept
    try:
        count = sum(asset.get("download_count", 0) for release in _releases() for asset in release.get("assets") or []
                    if DOWNLOAD.match(asset.get("name") or ""))
    except (OSError, ValueError, TypeError, AttributeError):
        return kept
    _db.put_item(TableName=STATS, Item={"id": {"S": "downloads"}, "count": {"N": str(count)}, "at": {"N": str(int(time.time()))}})
    return count


def _releases() -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for page in range(1, 11):
        request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": "role-radar-badge"})
        with urllib.request.urlopen(request, timeout=3) as response:
            batch = json.load(response)
        releases += batch
        if len(batch) < 100:
            break
    return releases


def _reply(status: int, message: str) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps({"ok": status == 200, "message": message})}
