"""Role Radar's suggestions box: the function URL the app's Companies page posts to (role_radar/suggest.py).

Each POST is one suggestion, JSON: {"name", "url"?, "note"?, "profession"?, "countries"?, "app"?}.
The same company (by its careers page, else by name) is kept once, counting how many asked;
`role-radar suggestions` lists them. Nothing is read back here: the box only takes suggestions in.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import boto3

TABLE = os.environ.get("TABLE", "role-radar-suggestions")
LIMITS = {"name": 120, "url": 500, "note": 1000, "profession": 20, "app": 40}
MAX_BODY = 8000
_db = None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    if (event.get("requestContext") or {}).get("http", {}).get("method") != "POST":
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


def _reply(status: int, message: str) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps({"ok": status == 200, "message": message})}
