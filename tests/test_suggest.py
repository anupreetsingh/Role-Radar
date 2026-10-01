"""Suggestions: the app sends companies for everyone's lists; the box keeps them; the maintainer lists them."""

import importlib.util
import io
import json
from pathlib import Path

import httpx
import pytest

from role_radar import cli, onboarding, suggest

BOX = Path(__file__).parents[1] / "deploy" / "suggestions" / "app.py"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "Role Radar" / "companies.yaml"
    onboarding.init(path)
    return path


@pytest.fixture
def posted(monkeypatch):
    """What reaches the box (none, while `down` is set)."""
    sent = {"bodies": [], "down": False}

    def post(url, json=None, **kw):
        if sent["down"]:
            raise httpx.ConnectError("offline")
        sent["bodies"].append(json)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(suggest.httpx, "post", post)
    monkeypatch.setenv("ROLE_RADAR_SUGGESTIONS_URL", "https://box.example/")
    return sent


def test_a_suggestion_that_cant_go_waits_for_the_next(config, posted):
    onboarding.save_profession(config, "accounting")
    onboarding.save_profile(config, ["accountant"], [], [], 2, countries=["CA"])
    posted["down"] = True
    result = suggest.suggest(config, "Deloitte Canada", "https://careers.deloitte.ca", "Big 4")
    assert result["sent"] == 0 and result["waiting"] == 1 and "next one" in result["error"]
    posted["down"] = False
    result = suggest.suggest(config, "MNP", "")
    assert result == {"sent": 2, "waiting": 0, "error": None}
    first, second = posted["bodies"]
    assert (first["name"], first["url"], first["note"], first["profession"], first["countries"]) == (
        "Deloitte Canada", "https://careers.deloitte.ca", "Big 4", "accounting", ["CA"])
    assert second["name"] == "MNP" and second["url"] is None
    assert not suggest.waiting_file(config).exists()


def test_without_a_box_suggestions_are_kept(config, monkeypatch):
    monkeypatch.delenv("ROLE_RADAR_SUGGESTIONS_URL", raising=False)
    monkeypatch.setattr(suggest, "ENDPOINT", "")
    result = suggest.suggest(config, "MNP")
    assert result["waiting"] == 1 and "isn't set up" in result["error"]
    with pytest.raises(ValueError, match="https://"):
        suggest.suggest(config, "MNP", "mnp.ca careers")
    with pytest.raises(ValueError, match="name"):
        suggest.suggest(config, " ")


def test_adding_an_unreadable_company_can_suggest_it_too(config, posted, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"name": "Own Site", "url": "https://www.ownsite.example/careers", "suggest": True})))
    assert cli.main(["setup", "add", "--config", str(config)]) == 0
    assert [b["name"] for b in posted["bodies"]] == ["Own Site"]
    assert posted["bodies"][0]["note"].startswith("couldn't add it")


def test_adding_a_company_suggests_it_unless_its_listed(config, posted, monkeypatch):
    onboarding.import_companies(config, "- name: Own Co\n  url: https://jobs.lever.co/own\n", check=False, directory={})
    for name in ("Nowhere Co", "Own Co"):  # unknown, then listed already
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"name": name})))
        assert cli.main(["setup", "add", "--config", str(config)]) == 0
    assert [(b["name"], b["note"]) for b in posted["bodies"]] == [("Nowhere Co", "couldn't add it: no careers page to read")]


@pytest.fixture
def box(fake_aws, monkeypatch):
    """The suggestions box's function and table, in fake AWS."""
    import boto3

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(TableName="role-radar-suggestions", BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])
    spec = importlib.util.spec_from_file_location("suggestions_box", BOX)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, client


def post(box, body, method="POST"):
    module, _ = box
    event = {"requestContext": {"http": {"method": method}},
             "body": body if isinstance(body, str) else json.dumps(body)}
    return module.handler(event)


def test_the_box_keeps_each_company_once_counting_who_asked(box):
    _, client = box
    asked = {"name": "Deloitte Canada", "url": "https://careers.deloitte.ca/", "profession": "accounting",
             "countries": ["CA"], "note": "Big 4", "app": "2.1.0"}
    assert post(box, asked)["statusCode"] == 200
    assert post(box, {**asked, "url": "https://CAREERS.deloitte.ca", "countries": ["US", "CA"], "note": ""})["statusCode"] == 200
    assert post(box, {"name": "MNP"})["statusCode"] == 200
    rows = suggest.list_suggestions(client=client)
    assert [(r["name"], r["requests"]) for r in rows] == [("Deloitte Canada", 2), ("MNP", 1)]
    deloitte = rows[0]
    assert deloitte["id"] == "careers.deloitte.ca" and deloitte["countries"] == ["CA", "US"]
    assert deloitte["professions"] == ["accounting"] and len(deloitte["notes"]) == 1 and deloitte["status"] == "new"
    assert rows[1]["id"] == "name:mnp"

    suggest.mark_done(["name:mnp"], client=client)
    assert [r["name"] for r in suggest.list_suggestions(client=client)] == ["Deloitte Canada"]
    assert len(suggest.list_suggestions(everything=True, client=client)) == 2


@pytest.mark.parametrize("body, status", [
    ({"url": "https://x.example"}, 400),  # no name
    ({"name": "X", "url": "ftp://x"}, 400),
    ({"name": "X" * 200}, 400),
    ("not json", 400),
    ("[1, 2]", 400),
    ({"name": "X", "note": "y" * 9000}, 413),
])
def test_the_box_turns_away_what_isnt_a_suggestion(box, body, status):
    assert post(box, body)["statusCode"] == status


def test_the_box_only_takes_posts(box):
    assert post(box, {"name": "X"}, method="GET")["statusCode"] == 405


def test_the_app_checks_in_with_the_same_id_and_nothing_else(config, monkeypatch):
    """The counts on the GitHub page: a random id made once on the Mac, the app's and macOS's versions."""
    from role_radar import __version__

    sent = []

    def post(url, json=None, **kw):
        sent.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(suggest.httpx, "post", post)
    monkeypatch.setenv("ROLE_RADAR_SUGGESTIONS_URL", "https://box.example/")
    assert cli.main(["checkin", "--config", str(config)]) == 0
    assert suggest.check_in(config)
    (url, first), (_, second) = sent
    assert url == "https://box.example/checkin" and set(first) == {"id", "app", "os"}
    assert first["id"] == second["id"] and first["app"] == __version__
    assert (config.parent / "install-id").read_text().strip() == first["id"]

    def offline(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(suggest.httpx, "post", offline)
    assert cli.main(["checkin", "--config", str(config)]) == 1  # quietly: the next one counts all the same


def call(box, method, path, body=None):
    module, _ = box
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path}
    if body is not None:
        event["body"] = json.dumps(body)
    return module.handler(event)


def make_table(client, name):
    client.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])


def test_the_box_counts_weekly_users(box):
    import uuid

    _, client = box
    make_table(client, "role-radar-installs")
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    for install in (mine, mine, theirs):
        assert call(box, "POST", "/checkin", {"id": install, "app": "2.1.2", "os": "15.1"})["statusCode"] == 200
    assert call(box, "POST", "/checkin", {"id": "not-an-id"})["statusCode"] == 400
    row = client.get_item(TableName="role-radar-installs", Key={"id": {"S": mine}})["Item"]
    assert (row["checkins"]["N"], row["app"]["S"], row["os"]["S"]) == ("2", "2.1.2", "15.1")
    assert json.loads(call(box, "GET", "/badge/users")["body"]) == {
        "schemaVersion": 1, "label": "weekly users", "message": "2", "color": "blue", "cacheSeconds": 3600}
    # A copy last seen weeks ago no longer counts.
    client.update_item(TableName="role-radar-installs", Key={"id": {"S": theirs}}, UpdateExpression="SET last_seen = :old",
                       ExpressionAttributeValues={":old": {"S": "2026-01-01T00:00:00Z"}})
    assert json.loads(call(box, "GET", "/badge/users/")["body"])["message"] == "1"
    # Suggestions work as before, and never land among the installs.
    assert call(box, "POST", "/", {"name": "MNP"})["statusCode"] == 200
    assert call(box, "GET", "/")["statusCode"] == 405
    assert client.scan(TableName="role-radar-installs")["Count"] == 2
    assert client.scan(TableName="role-radar-suggestions")["Count"] == 1


def test_the_downloads_badge_counts_fresh_downloads_only(box, monkeypatch):
    """The disk image as people download it, every version (older releases named it by version); not its
    ...-update.dmg copy, nor the appcast."""
    module, client = box
    make_table(client, "role-radar-stats")
    releases = [{"assets": [{"name": "Role-Radar-apple-silicon.dmg", "download_count": 3},
                            {"name": "Role-Radar-2.1.3-update.dmg", "download_count": 40},
                            {"name": "appcast.xml", "download_count": 500}]},
                {"assets": [{"name": "Role-Radar-2.1.0-apple-silicon.dmg", "download_count": 8}]}]
    asked = []

    def from_github():
        asked.append(1)
        return releases

    monkeypatch.setattr(module, "_releases", from_github)
    badge = lambda: json.loads(call(box, "GET", "/badge/downloads")["body"])  # noqa: E731
    assert badge() == {"schemaVersion": 1, "label": "downloads", "message": "11", "color": "blue", "cacheSeconds": 3600}
    releases[0]["assets"][0]["download_count"] = 5
    assert badge()["message"] == "11" and len(asked) == 1  # GitHub is asked at most every REFRESH seconds
    monkeypatch.setattr(module, "REFRESH", 0)
    assert badge()["message"] == "13"

    def down():
        raise OSError("rate limited")

    monkeypatch.setattr(module, "_releases", down)
    assert badge()["message"] == "13"  # the last count, while GitHub can't answer
