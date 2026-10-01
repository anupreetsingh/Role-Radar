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
