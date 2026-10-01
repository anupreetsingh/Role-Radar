"""Live Tracking: the list of matches waiting to be sent, skipping, "Send now", and digests during a long pass."""

import asyncio
import io
import json
from datetime import timedelta

import httpx
import pytest

from role_radar import cli, monitor, tracker, ui
from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.filters import JobFilter
from role_radar.http_client import HttpSettings
from role_radar.storage import (
    SKIPPED, CompanyMeta, DigestSchedule, JsonStateStore, MemoryStateStore, MonitorState, QueuedMatch, SeenJob, to_iso, utcnow,
)
from tests.conftest import fixture_json
from tests.test_cli import config  # noqa: F401  (the CLI's fixture)
from tests.test_monitor import RecordingNotifier, run_monitor

# In the future, so the runner's clock is ahead of the stores' real one, as it never is behind in use.
T = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0)
DEVELOPER = "Mid/Senior Software Developer (.NET Core / React / AWS)"


def settings(**kw):
    return Settings(digest_interval_minutes=30, check_interval_minutes=1440,
                    http=HttpSettings(per_domain_delay=0, respect_robots=False, max_retries=0), **kw)


def companies(*names):
    return AppConfig(
        settings=settings(),
        companies=[CompanyConfig(name=name, url=f"https://{name.lower()}.bamboohr.com/careers",
                                 filter=JobFilter(include_keywords=["software developer", "data engineer"])) for name in names],
    )


@pytest.fixture(params=["json", "sqlite", "dynamodb"])
def store_factory(request, tmp_path):
    if request.param == "json":
        return lambda: JsonStateStore(tmp_path / "state.json")
    if request.param == "sqlite":
        from role_radar.sqlite import SqliteStateStore

        return lambda: SqliteStateStore(tmp_path / "state.db")
    from role_radar.dynamo import DynamoStateStore

    client, table = request.getfixturevalue("table")
    return lambda: DynamoStateStore(client, table)


def at(minutes):
    return lambda: T + timedelta(minutes=minutes)


def test_a_skipped_match_is_recorded_not_sent(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    waiting = store_factory().load_queue()
    assert sorted(m.title for m in waiting) == ["Data Engineer", DEVELOPER]
    skip = next(m for m in waiting if m.title == "Data Engineer")
    assert store_factory().mark_skipped("Acme", skip.uid, True)

    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))
    (batch,) = notifier.batches
    assert [j.title for j in batch] == [DEVELOPER]
    job = store_factory().load_company("Acme").jobs[skip.uid]
    assert job.dropped_for == SKIPPED and job.notified_at
    (done,) = store_factory().load_queue()  # the sent one left the list; the skip stays, applied
    assert (done.uid, bool(done.skipped_at), bool(done.done_at)) == (skip.uid, True, True)
    assert not store_factory().mark_skipped("Acme", skip.uid, True)  # seen already
    assert not store_factory().mark_send("Acme", skip.uid)

    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(60))
    assert len(notifier.batches) == 1


def test_a_recorded_skip_can_be_put_back(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    skip = next(m for m in store_factory().load_queue() if m.title == "Data Engineer")
    assert store_factory().mark_skipped("Acme", skip.uid, True)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))  # sends the other, records the skip
    assert len(notifier.batches) == 1 and store_factory().load_company("Acme").jobs[skip.uid].dropped_for == SKIPPED

    assert store_factory().mark_skipped("Acme", skip.uid, False)  # Mark as New: listed as new at once
    assert [m["uid"] for m in ui.live(store_factory())["waiting"]] == [skip.uid] and not ui.live(store_factory())["skipped"]
    assert store_factory().mark_skipped("Acme", skip.uid, True)  # and back, before the digest
    (seen,) = ui.live(store_factory())["skipped"]
    assert seen["uid"] == skip.uid and seen["skipped_at"]
    assert store_factory().mark_skipped("Acme", skip.uid, False)

    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(60))  # the digest puts it back, and sends it
    assert [[j.uid for j in batch] for batch in notifier.batches][1:] == [[skip.uid]]
    job = store_factory().load_company("Acme").jobs[skip.uid]
    assert job.notified_at and not job.dropped_for
    assert store_factory().load_queue() == []


def test_with_alerts_off_a_match_put_back_waits_again(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    store_factory().save_switch("discord", False)
    store_factory().save_switch("email", False)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    skip, keep = sorted(store_factory().load_queue(), key=lambda m: m.title)
    assert store_factory().mark_skipped("Acme", skip.uid, True)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))
    assert [m["final"] for m in ui.live(store_factory())["skipped"]] == [True]

    assert store_factory().mark_skipped("Acme", skip.uid, False)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(60))
    assert notifier.batches == []
    (back, other) = sorted(store_factory().load_queue(), key=lambda m: m.title)
    assert (back.uid, back.skipped_at, back.done_at, back.restore_at) == (skip.uid, None, None, None)
    assert store_factory().load_company("Acme").jobs[skip.uid].dropped_for is None

    assert store_factory().mark_send("Acme", skip.uid)  # waiting like any other: it can be sent
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(90))
    assert [[j.uid for j in batch] for batch in notifier.batches] == [[skip.uid]]
    assert [m.uid for m in store_factory().load_queue()] == [keep.uid]


def test_a_match_marked_as_new_can_be_sent_before_the_digest_puts_it_back(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    store_factory().save_switch("discord", False)
    store_factory().save_switch("email", False)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    skip = store_factory().load_queue()[0]
    assert store_factory().mark_skipped("Acme", skip.uid, True)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))
    assert store_factory().mark_skipped("Acme", skip.uid, False)
    assert store_factory().mark_send("Acme", skip.uid)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(60))
    assert [[j.uid for j in batch] for batch in notifier.batches] == [[skip.uid]]
    assert skip.uid not in {m.uid for m in store_factory().load_queue()}


def test_a_skip_can_be_undone_until_the_digest(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    uid = store_factory().load_queue()[0].uid
    assert store_factory().mark_skipped("Acme", uid, True)
    assert store_factory().mark_skipped("Acme", uid, False)
    assert not any(m.skipped_at for m in store_factory().load_queue())
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))
    assert len(notifier.batches[0]) == 2
    assert store_factory().load_queue() == []
    assert not store_factory().mark_skipped("Acme", uid, True)  # sent: nothing to skip


def test_with_alerts_off_the_list_grows_and_goes_out_once_one_is_back_on(monkeypatch, store_factory):
    cfg, notifier = companies("Acme", "Other"), RecordingNotifier()
    store = store_factory()
    store.save_switch("discord", False)
    store.save_switch("email", False)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))
    assert notifier.batches == []
    waiting = ui.live(store_factory())["waiting"]
    assert len(waiting) == 4 and ui.live(store_factory())["alerts_off"]
    skip = waiting[0]
    assert store_factory().mark_skipped(skip["company"], skip["uid"], True)

    store_factory().save_switch("email", True)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(60))
    (batch,) = notifier.batches
    assert len(batch) == 3 and skip["uid"] not in {j.uid for j in batch}
    live = ui.live(store_factory())
    assert live["waiting"] == [] and [m["uid"] for m in live["skipped"]] == [skip["uid"]] and live["skipped"][0]["final"]


def test_matches_sent_from_live_tracking_go_before_the_digest_time(monkeypatch, store_factory):
    cfg, notifier = companies("Acme"), RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    first, second = store_factory().load_queue()
    assert store_factory().mark_send("Acme", first.uid)
    store_factory().request_digest()
    assert ui.live(store_factory())["send_requested"]
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(5))
    assert [[j.uid for j in batch] for batch in notifier.batches] == [[first.uid]]  # only the one sent
    assert not ui.live(store_factory())["send_requested"]
    assert [m["uid"] for m in ui.live(store_factory())["waiting"]] == [second.uid]
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(6))
    assert len(notifier.batches) == 1
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))  # the digest sends the rest
    assert [[j.uid for j in batch] for batch in notifier.batches] == [[first.uid], [second.uid]]


def test_with_alerts_off_matches_collect_and_only_those_sent_go_out(monkeypatch, store_factory):
    cfg, notifier = companies("Acme", "Other"), RecordingNotifier()
    store_factory().save_switch("discord", False)
    store_factory().save_switch("email", False)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(0))
    send, clear, *rest = ui.live(store_factory())["waiting"]
    assert store_factory().mark_send(send["company"], send["uid"])
    assert store_factory().mark_skipped(clear["company"], clear["uid"], True)
    assert not store_factory().mark_send(clear["company"], clear["uid"])  # cleared: not sendable
    store_factory().request_digest()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(5))
    assert [[j.uid for j in batch] for batch in notifier.batches] == [[send["uid"]]]  # to every channel set up
    live = ui.live(store_factory())
    assert [m["uid"] for m in live["waiting"]] == [m["uid"] for m in rest] and live["alerts_off"]
    assert [(m["uid"], m["final"]) for m in live["skipped"]] == [(clear["uid"], True)]  # recorded, alerts off
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=at(30))  # the digest time: nothing more
    assert len(notifier.batches) == 1 and len(ui.live(store_factory())["waiting"]) == 2


def test_the_digest_lists_the_newest_matches_first(monkeypatch):
    store, notifier = MemoryStateStore(), RecordingNotifier()
    for minutes, names in ((0, ("Zeta",)), (10, ("Zeta", "Acme")), (30, ("Zeta", "Acme"))):  # Acme's jobs are found later
        monkeypatch.setattr(tracker, "utcnow", at(minutes))  # when a job is first seen
        run_monitor(monkeypatch, store, notifier, config=companies(*names), clock=at(minutes))
    (batch,) = notifier.batches
    assert [j.company for j in batch] == ["Acme", "Acme", "Zeta", "Zeta"]
    (alert,) = ui.live(store)["sent"]
    assert [j["company"] for j in alert["jobs"]] == ["Acme", "Acme", "Zeta", "Zeta"]
    assert alert["sent_at"] == to_iso(T + timedelta(minutes=30))


def test_sent_alerts_are_grouped_leaving_out_one_the_log_may_have_cut_short():
    rows = [{"notified_at": "2026-09-29T19:50:00Z", "company": "B", "title": "One", "first_seen": "2026-09-29T19:45:00Z", "by": "laptop:mac"},
            {"notified_at": "2026-09-29T19:50:00Z", "company": "A", "title": "Two", "first_seen": "2026-09-29T19:48:00Z", "by": "laptop:mac"},
            {"notified_at": "2026-09-29T19:40:00Z", "company": "C", "title": "Three", "by": "lambda"},
            {"notified_at": "2026-09-29T19:30:00Z", "company": "D", "title": "Four", "by": "lambda"}]
    alerts = ui.sent_alerts(rows, limit=10)
    assert [(a["sent_at"][11:16], a["by"], [j["title"] for j in a["jobs"]]) for a in alerts] == [
        ("19:50", "laptop", ["Two", "One"]), ("19:40", "lambda", ["Three"]), ("19:30", "lambda", ["Four"])]
    assert [a["sent_at"][11:16] for a in ui.sent_alerts(rows, limit=4)] == ["19:50", "19:40"]


def test_the_digest_goes_out_during_a_long_pass_leaving_a_company_being_checked(monkeypatch):
    """A digest due mid-pass doesn't wait for the pass, and doesn't touch a company whose check is in flight."""
    monkeypatch.setattr(monitor, "TICK", 0.02)
    cfg = companies("Fast", "Slow")
    old = SeenJob(title="Older match", url="https://slow.example/1", fingerprint="f", first_seen=to_iso(T - timedelta(days=1)),
                  matched=True)
    store = MemoryStateStore(MonitorState(
        companies={"Slow": {"slow:test:old": old}},
        meta={"Slow": CompanyMeta(last_ok_at=to_iso(T - timedelta(days=1)), next_check_at=to_iso(T), pending=1)},
    ))
    store.save_digest(DigestSchedule(next_send_at=to_iso(T + timedelta(minutes=1)), interval_minutes=30))
    clock = [T]
    slow_done = asyncio.Event()

    class Recording(RecordingNotifier):
        async def send(self, jobs):
            self.slow_done_at_send = slow_done.is_set()
            await super().send(jobs)

    async def handler(request):
        if request.url.host.startswith("slow"):
            await asyncio.sleep(0.2)  # Fast has been checked and saved by now
            clock[0] = T + timedelta(minutes=1)  # the digest is due
            await asyncio.sleep(0.5)
            slow_done.set()
        return httpx.Response(200, json=fixture_json("bamboohr_list.json"))

    notifier = Recording()
    assert run_monitor(monkeypatch, store, notifier, config=cfg, handler=handler, clock=lambda: clock[0]) == 0
    (batch,) = notifier.batches
    assert {j.company for j in batch} == {"Fast"} and len(batch) == 2 and not notifier.slow_done_at_send
    slow = store.load_company("Slow")
    assert slow.pending_count() == 3 and slow.meta.pending == 3  # for the next digest
    assert store.load_digest().next_send_at == to_iso(T + timedelta(minutes=30))


def test_a_pass_reports_its_progress(monkeypatch):
    reports = []
    store = MemoryStateStore()
    run_monitor(monkeypatch, store, RecordingNotifier(), config=companies("Acme", "Other"), clock=at(0), progress=reports.append)
    first, last = reports[0], reports[-1]
    assert (first["total"], first["done"], first["started_at"]) == (2, 0, to_iso(T))
    assert (last["done"], last["finished_at"]) == (2, to_iso(T))
    run_monitor(monkeypatch, store, RecordingNotifier(), config=companies("Acme", "Other"), clock=at(1), progress=reports.append)
    assert len(reports) == 2  # nothing was due: no round


def test_latest_round_marks_one_that_stopped_reporting():
    now = T
    rounds = {"laptop:mac": {"started_at": to_iso(now - timedelta(minutes=9)), "updated_at": to_iso(now - timedelta(minutes=5)),
                             "total": 10, "done": 4},
              "lambda": {"started_at": to_iso(now - timedelta(hours=1)), "updated_at": to_iso(now - timedelta(hours=1)),
                         "finished_at": to_iso(now - timedelta(hours=1)), "total": 3, "done": 3}}
    latest = ui.latest_round(rounds, now)
    assert (latest["runner"], latest["stale"], latest["done"]) == ("laptop", True, 4)
    rounds["laptop:mac"]["updated_at"] = to_iso(now - timedelta(seconds=20))
    assert not ui.latest_round(rounds, now)["stale"]
    assert ui.latest_round({}, now) is None


# -- DynamoDB ----------------------------------------------------------------------


def test_saves_keep_the_users_skip_and_the_send_now_row(table):
    from role_radar.dynamo import DynamoStateStore
    from tests.test_dynamo import add_jobs

    client, name = table
    store = DynamoStateStore(client, name)
    record = store.load_company("Acme")
    (first,) = add_jobs(record, "Software Engineer")
    store.save_company(record)
    assert store.mark_skipped("Acme", first.uid, True)

    record = store.load_company("Acme")
    record.jobs[first.uid].location = "Remote"  # a later check changes the job: its row is updated...
    store.save_company(record)
    (row,) = store.load_queue()
    assert row.location == "Remote" and row.skipped_at  # ...and the skip survives
    assert not store.mark_send("Acme", first.uid)  # skipped: not sendable
    assert store.mark_skipped("Acme", first.uid, False) and store.mark_send("Acme", first.uid)
    record = store.load_company("Acme")
    record.jobs[first.uid].location = "Austin, TX"
    store.save_company(record)
    (row,) = store.load_queue()
    assert row.location == "Austin, TX" and row.send_at  # so does a send

    store.save_digest(DigestSchedule(next_send_at="2026-09-01T00:30:00Z", interval_minutes=30))
    store.request_digest()
    store.save_digest(DigestSchedule(next_send_at="2026-09-01T01:00:00Z", interval_minutes=30))  # the runner's own row
    assert store.load_digest().requested and store.load().digest.requested_at

    store.repair_queue([QueuedMatch("Other", "other:test:1", "Data Engineer", "https://o.example/1", "2026-09-01T00:00:00Z")],
                       [("Acme", first.uid)])
    assert [(m.company, m.uid) for m in store.load_queue()] == [("Other", "other:test:1")]

    store.record_round("laptop:mac", {"started_at": "2026-09-01T00:00:00Z", "total": 5, "done": 2})
    assert store.load_rounds() == {"laptop:mac": {"started_at": "2026-09-01T00:00:00Z", "total": 5, "done": 2}}


# -- the command line ----------------------------------------------------------------


def test_matches_command_lists_and_skips_before_alerts_go_out(config, capsys):  # noqa: F811
    run = ["--config", str(config)]
    assert cli.main(["switch", "discord", "off", *run]) == 0
    assert cli.main(["switch", "email", "off", *run]) == 0
    assert cli.main(["run", "--once", *run]) == 0
    capsys.readouterr()

    assert cli.main(["matches", "--json", *run]) == 0
    live = json.loads(capsys.readouterr().out)
    assert sorted(m["title"] for m in live["waiting"]) == ["Data Engineer", DEVELOPER] and live["alerts_off"]
    skip = next(m for m in live["waiting"] if m["title"] == "Data Engineer")
    assert cli.main(["matches", "skip", skip["company"], skip["uid"], *run]) == 0
    out = capsys.readouterr().out
    assert "Waiting to be sent (1)" in out and "Skipped (1)" in out and "until the next digest" in out

    # Without a digest, the next check sends what's waiting once an alert is back on, the skipped one excepted.
    assert cli.main(["switch", "email", "on", *run]) == 0
    assert cli.main(["run", "--once", "--all", *run]) == 0
    out = capsys.readouterr().out
    assert DEVELOPER in out and "Data Engineer" not in out  # alerts print here; the log goes to stderr
    assert cli.main(["matches", "skip", skip["company"], skip["uid"], *run]) == 0
    assert "Not waiting any more" in capsys.readouterr().err


def test_matches_command_sends_and_clears_picked_matches(config, capsys):  # noqa: F811
    run = ["--config", str(config)]
    assert cli.main(["switch", "discord", "off", *run]) == 0
    assert cli.main(["switch", "email", "off", *run]) == 0
    assert cli.main(["run", "--once", *run]) == 0
    capsys.readouterr()
    assert cli.main(["matches", "--json", *run]) == 0
    first, second = json.loads(capsys.readouterr().out)["waiting"]

    assert cli.main(["matches", "send", "--pick", first["company"], first["uid"], "--json", *run]) == 0
    live = json.loads(capsys.readouterr().out)
    assert live["send_requested"] and {m["uid"]: bool(m["send_at"]) for m in live["waiting"]} == {
        first["uid"]: True, second["uid"]: False}
    assert cli.main(["matches", "skip", "--pick", second["company"], second["uid"],
                     "--pick", first["company"], first["uid"], "--json", *run]) == 0
    live = json.loads(capsys.readouterr().out)  # clearing one on its way out stops it: the skip wins
    assert live["waiting"] == [] and {m["uid"] for m in live["skipped"]} == {first["uid"], second["uid"]}


def test_matches_command_takes_a_big_selection_on_stdin(config, capsys, monkeypatch):  # noqa: F811
    run = ["--config", str(config)]
    assert cli.main(["switch", "discord", "off", *run]) == 0
    assert cli.main(["switch", "email", "off", *run]) == 0
    assert cli.main(["run", "--once", *run]) == 0
    capsys.readouterr()
    assert cli.main(["matches", "--json", *run]) == 0
    waiting = json.loads(capsys.readouterr().out)["waiting"]
    picks = [[m["company"], m["uid"]] for m in waiting] + [["Gone Co", "gone:1"]] * 3000  # far past 4,096 arguments

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(picks)))
    assert cli.main(["matches", "skip", "--stdin", "--json", *run]) == 0
    live = json.loads(capsys.readouterr().out)
    assert live["waiting"] == [] and len(live["skipped"]) == len(waiting)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"company": "Acme"}'))
    assert cli.main(["matches", "skip", "--stdin", *run]) == cli.EXIT_USAGE


def test_matches_command_needs_a_match_to_skip(config):  # noqa: F811
    assert cli.main(["matches", "skip", "--config", str(config)]) == cli.EXIT_USAGE
