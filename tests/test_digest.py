from datetime import timedelta

import pytest

from role_radar import monitor
from role_radar.config import AppConfig, CompanyConfig, Settings
from role_radar.filters import JobFilter
from role_radar.http_client import HttpSettings
from role_radar.storage import JsonStateStore, from_iso
from tests.test_monitor import FlakyStore, RecordingNotifier, T0, run_monitor


def config(*names):
    return AppConfig(
        settings=Settings(digest_interval_minutes=30, check_interval_minutes=1440,
                          http=HttpSettings(per_domain_delay=0, respect_robots=False, max_retries=0)),
        companies=[CompanyConfig(name=name, url="https://contfinco.bamboohr.com/careers",
                                 filter=JobFilter(include_keywords=["software developer", "data engineer"])) for name in names],
    )


@pytest.fixture(params=["json", "dynamodb"])
def store_factory(request, tmp_path):
    if request.param == "json":
        return lambda: JsonStateStore(tmp_path / "state.json")
    from role_radar.dynamo import DynamoStateStore

    client, table = request.getfixturevalue("table")
    return lambda: DynamoStateStore(client, table)


def test_digest_batches_companies_across_restarts_without_due_scrapes(monkeypatch, store_factory):
    cfg = config("Continental Finance", "Other")
    notifier = RecordingNotifier()
    assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg) == 0
    assert notifier.batches == []
    assert from_iso(store_factory().load_digest().next_send_at) == T0 + timedelta(minutes=30)

    # A fresh process every time, with neither company due for another day.
    for minutes in (5, 29):
        assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=minutes)) == 0
        assert notifier.batches == []
    requests = []
    assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg, requested=requests,
                       clock=lambda: T0 + timedelta(minutes=30)) == 0
    assert requests == [] and len(notifier.batches) == 1
    assert len(notifier.batches[0]) == 4
    assert {j.company for j in notifier.batches[0]} == {"Continental Finance", "Other"}
    assert all(j.url for j in notifier.batches[0])

    # No duplicate digest within the window, and no empty digest next window.
    for minutes in (31, 60, 90):
        assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=minutes)) == 0
    assert len(notifier.batches) == 1


def test_partial_digest_retries_only_failed_channel_at_next_interval(monkeypatch, store_factory):
    cfg = config("Continental Finance", "Other")
    discord, email = RecordingNotifier(), RecordingNotifier(fail=True)
    discord.name, email.name = "discord", "email"
    channels = [discord, email]
    assert run_monitor(monkeypatch, store_factory(), channels, config=cfg) == 0
    assert run_monitor(monkeypatch, store_factory(), channels, config=cfg, clock=lambda: T0 + timedelta(minutes=30)) == 1
    assert len(discord.batches) == 1 and len(discord.batches[0]) == 4 and not email.batches
    email.fail = False
    assert run_monitor(monkeypatch, store_factory(), channels, config=cfg, clock=lambda: T0 + timedelta(minutes=35)) == 0
    assert not email.batches
    assert run_monitor(monkeypatch, store_factory(), channels, config=cfg, clock=lambda: T0 + timedelta(minutes=60)) == 0
    assert len(discord.batches) == len(email.batches) == 1
    assert len(email.batches[0]) == 4
    for name in ("Continental Finance", "Other"):
        assert all(j.notified_at and set(j.notified_channels) == {"discord", "email"}
                   for j in store_factory().load_company(name).jobs.values() if j.matched)


def test_jobs_found_in_separate_passes_share_one_digest(monkeypatch, store_factory):
    cfg = config("Continental Finance", "Other")
    cfg.companies[1].enabled = False
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg)
    cfg.companies[1].enabled = True
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=15))
    assert notifier.batches == []
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=30))
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 4


def test_digest_survives_a_careers_site_failure(monkeypatch, store_factory):
    import httpx

    cfg = config("Continental Finance")
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg)
    # The previously found jobs must still be sent even if the current scrape fails.
    assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg, check_all=True,
                       handler=lambda request: httpx.Response(503), clock=lambda: T0 + timedelta(minutes=30)) == 1
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 2


def test_digest_is_sent_when_the_work_window_ends_mid_pass(monkeypatch, store_factory):
    # Lambda stops starting companies after its work window. With a backlog every
    # pass runs into it, and the digest must still go out.
    cfg = config("Continental Finance")
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg)
    calls = []

    def window_ends_after_first_company():
        calls.append(1)
        return len(calls) > 1

    assert run_monitor(monkeypatch, store_factory(), notifier, config=cfg, check_all=True,
                       should_stop=window_ends_after_first_company, clock=lambda: T0 + timedelta(minutes=30)) == 0
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 2


def spy_loads(store, loaded):
    original = store.load_company

    def load_company(name):
        loaded.append(name)
        return original(name)

    store.load_company = load_company
    return store


def test_digest_reads_only_companies_with_pending_matches(monkeypatch, store_factory):
    cfg = config("Continental Finance", "Other", "Quiet")
    cfg.companies[2].filter = JobFilter(include_keywords=["no such role"])
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg)

    loaded = []
    run_monitor(monkeypatch, spy_loads(store_factory(), loaded), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=30))
    assert sorted(loaded) == ["Continental Finance", "Other"]  # "Quiet" has no matches
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 4

    loaded.clear()
    run_monitor(monkeypatch, spy_loads(store_factory(), loaded), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=60))
    assert loaded == [] and len(notifier.batches) == 1  # everything was delivered


def test_digest_counts_pending_matches_of_older_rows_at_their_next_check(monkeypatch):
    from role_radar.storage import MemoryStateStore

    cfg = config("Continental Finance", "Other")
    store, notifier = MemoryStateStore(), RecordingNotifier()
    run_monitor(monkeypatch, store, notifier, config=cfg)
    store.state.meta["Other"].pending = None  # saved before pending counts were kept

    run_monitor(monkeypatch, store, notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=30))
    assert [{j.company for j in batch} for batch in notifier.batches] == [{"Continental Finance"}]

    run_monitor(monkeypatch, store, notifier, config=cfg, only={"other"}, clock=lambda: T0 + timedelta(minutes=40))
    run_monitor(monkeypatch, store, notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=60))
    assert [{j.company for j in batch} for batch in notifier.batches] == [{"Continental Finance"}, {"Other"}]


def test_digest_skips_baselines_and_dry_runs(monkeypatch, store_factory):
    cfg = config("Continental Finance")
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, dry_run=True)
    assert not store_factory().load_company("Continental Finance").jobs
    assert store_factory().load_digest().next_send_at is None
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, baseline=True)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=1))
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=30))
    assert notifier.batches == []


def test_digest_deduplicates_matching_fingerprints(monkeypatch, store_factory):
    from dataclasses import replace

    cfg = config("Continental Finance")
    notifier = RecordingNotifier()
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg)
    store = store_factory()
    record = store.load_company("Continental Finance")
    original = next(j for j in record.jobs.values() if j.matched)
    record.jobs["a-new-id"] = replace(original, notified_channels={})
    store.save_company(record)
    run_monitor(monkeypatch, store_factory(), notifier, config=cfg, clock=lambda: T0 + timedelta(minutes=30))
    assert len(notifier.batches) == 1 and len(notifier.batches[0]) == 2
    assert sum(j.duplicate_of is not None for j in store_factory().load_company("Continental Finance").jobs.values()) == 1


def test_digest_receipts_survive_failed_save_in_warm_runner(monkeypatch):
    monkeypatch.setattr(monitor, "SAVE_RETRIES_AFTER_ALERTS", 1)
    monkeypatch.setattr(monitor, "SAVE_RETRY_BASE", 0)
    monkeypatch.setattr(monitor, "UNSAVED_HOLD", 0)
    store, unsaved = FlakyStore(), monitor.Unsaved()
    cfg = config("Continental Finance")
    discord, email = RecordingNotifier(), RecordingNotifier()
    discord.name, email.name = "discord", "email"
    run_monitor(monkeypatch, store, [discord, email], config=cfg, unsaved=unsaved)
    store.failing = True
    assert run_monitor(monkeypatch, store, [discord, email], config=cfg, unsaved=unsaved,
                       clock=lambda: T0 + timedelta(minutes=30)) == 1
    assert len(discord.batches) == 1 and not email.batches
    store.failing = False
    assert run_monitor(monkeypatch, store, [discord, email], config=cfg, unsaved=unsaved,
                       clock=lambda: T0 + timedelta(minutes=60)) == 0
    assert len(discord.batches) == len(email.batches) == 1


@pytest.mark.parametrize("minutes", [-1, float("inf"), float("nan")])
def test_invalid_digest_interval_is_rejected(minutes):
    with pytest.raises(ValueError, match="digest_interval_minutes"):
        Settings(digest_interval_minutes=minutes)
