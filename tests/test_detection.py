from datetime import datetime, timedelta, timezone

from storage import JsonStateStore, MonitorState
from tracker import dedupe, mark_notified, reconcile
from tests.conftest import job

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def run(state, jobs, *, matching=None, now=T0, complete=True, notify=True, window=30):
    """Reconcile, then pretend notifications succeeded (as monitor.run does)."""
    matching = matching if matching is not None else {j.uid for j in jobs}
    diff = reconcile(
        state, "Acme", jobs, {j.uid: j.uid in matching for j in jobs},
        complete=complete, notify=notify, repost_window_days=window, now=now,
    )  # fmt: skip
    mark_notified(state, diff.to_notify, now)
    return diff


def test_new_job_alerts_once():
    state = MonitorState()
    a = job("Software Engineer", "1")
    assert [j.uid for j in run(state, [a]).to_notify] == [a.uid]
    second = run(state, [a], now=T0 + timedelta(minutes=30))
    assert second.to_notify == [] and second.unchanged == 1 and second.new == []


def test_non_matching_job_recorded_but_not_alerted():
    state = MonitorState()
    a = job("Recruiter", "9")
    diff = run(state, [a], matching=set())
    assert diff.to_notify == [] and len(diff.new) == 1
    assert state.jobs_for("Acme")[a.uid].matched is False


def test_only_new_jobs_alert_on_later_runs():
    state = MonitorState()
    a, b = job("Software Engineer", "1"), job("Data Engineer", "2")
    run(state, [a])
    diff = run(state, [a, b], now=T0 + timedelta(hours=1))
    assert [j.uid for j in diff.to_notify] == [b.uid]


def test_failed_notification_is_retried_next_run():
    state = MonitorState()
    a = job("Software Engineer", "1")
    reconcile(state, "Acme", [a], {a.uid: True}, now=T0)  # sent nothing: no mark_notified
    diff = run(state, [a], now=T0 + timedelta(minutes=30))
    assert [j.uid for j in diff.to_notify] == [a.uid]


def test_job_removed_and_returns_without_realert():
    state = MonitorState()
    a, b = job("Software Engineer", "1"), job("Data Engineer", "2")
    run(state, [a, b])
    diff = run(state, [b], now=T0 + timedelta(hours=1))
    assert diff.removed == [a.uid]
    assert state.jobs_for("Acme")[a.uid].removed_at is not None
    diff = run(state, [a, b], now=T0 + timedelta(hours=2))
    assert diff.returned == [a.uid] and diff.to_notify == []
    assert state.jobs_for("Acme")[a.uid].active


def test_incomplete_listing_does_not_mark_removed():
    state = MonitorState()
    a, b = job("Software Engineer", "1"), job("Data Engineer", "2")
    run(state, [a, b])
    diff = run(state, [b], complete=False, now=T0 + timedelta(hours=1))
    assert diff.removed == [] and state.jobs_for("Acme")[a.uid].active


def test_repost_with_new_id_within_window_is_suppressed():
    state = MonitorState()
    old = job("Backend Engineer", "100", location="Austin, TX")
    run(state, [old])
    run(state, [], now=T0 + timedelta(days=1))  # removed
    repost = job("Backend Engineer", "200", location="Austin, TX")
    diff = run(state, [repost], now=T0 + timedelta(days=5))
    assert diff.to_notify == []
    assert "repost" in diff.suppressed[0][1]
    assert state.jobs_for("Acme")[repost.uid].duplicate_of == old.uid


def test_id_swap_in_same_run_is_suppressed():
    state = MonitorState()
    run(state, [job("Backend Engineer", "100")])
    diff = run(state, [job("Backend Engineer", "200")], now=T0 + timedelta(hours=1))
    assert diff.to_notify == [] and len(diff.removed) == 1


def test_repost_after_window_alerts_again():
    state = MonitorState()
    run(state, [job("Backend Engineer", "100")])
    run(state, [], now=T0 + timedelta(days=1))
    diff = run(state, [job("Backend Engineer", "200")], now=T0 + timedelta(days=60))
    assert len(diff.to_notify) == 1


def test_duplicate_open_posting_is_suppressed():
    state = MonitorState()
    a = job("Data Engineer", "1", location="Remote")
    run(state, [a])
    dup = job("Data Engineer", "2", location="Remote")
    diff = run(state, [a, dup], now=T0 + timedelta(hours=1))
    assert diff.to_notify == [] and "duplicate" in diff.suppressed[0][1]


def test_duplicates_within_one_run_alert_once():
    state = MonitorState()
    diff = run(state, [job("Data Engineer", "1", location="Remote"), job("Data Engineer", "2", location="Remote")])
    assert len(diff.to_notify) == 1


def test_same_title_multiple_locations_are_separate_jobs():
    state = MonitorState()
    jobs = [job("Software Engineer", "1", location="Austin, TX"), job("Software Engineer", "2", location="Denver, CO")]
    diff = run(state, jobs)
    assert len(diff.to_notify) == 2


def test_dedupe_same_uid_in_listing():
    a = job("Software Engineer", "1")
    assert len(dedupe([a, job("Software Engineer", "1"), job("Other", "2")])) == 2


def test_baseline_records_without_alerting():
    state = MonitorState()
    a = job("Software Engineer", "1")
    diff = run(state, [a], notify=False)
    assert diff.to_notify == [] and state.jobs_for("Acme")[a.uid].notified_at
    assert run(state, [a], now=T0 + timedelta(hours=1)).to_notify == []


def test_filter_broadened_alerts_existing_unnotified_job():
    state = MonitorState()
    a = job("Site Reliability Engineer", "1")
    run(state, [a], matching=set())
    diff = run(state, [a], now=T0 + timedelta(hours=1))
    assert [j.uid for j in diff.to_notify] == [a.uid]


def test_prune_forgets_long_removed_jobs():
    state = MonitorState()
    run(state, [job("Software Engineer", "1")])
    run(state, [], now=T0 + timedelta(days=1))
    assert state.prune(retention_days=90, now=T0 + timedelta(days=30)) == 0
    assert state.prune(retention_days=90, now=T0 + timedelta(days=100)) == 1


def test_json_store_roundtrip_and_no_rewrite_when_unchanged(tmp_path):
    store = JsonStateStore(tmp_path / "seen.json")
    state = store.load()
    run(state, [job("Software Engineer", "1")])
    store.save(state)
    mtime = store.path.stat().st_mtime_ns
    reloaded = store.load()
    assert reloaded.to_dict() == state.to_dict()
    run(reloaded, [job("Software Engineer", "1")], now=T0 + timedelta(hours=1))  # nothing changed
    store.save(reloaded)
    assert store.path.stat().st_mtime_ns == mtime


def test_corrupt_state_file_raises(tmp_path):
    import pytest

    path = tmp_path / "seen.json"
    path.write_text("{not json")
    with pytest.raises(RuntimeError):
        JsonStateStore(path).load()
