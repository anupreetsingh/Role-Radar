"""DynamoStateStore and DynamoLease against fake DynamoDB (moto)."""

from datetime import datetime, timezone

import pytest

from role_radar.lease import LeaseLost
from role_radar.storage import CompanyMeta, DigestSchedule, MonitorState, SeenJob
from role_radar.tracker import reconcile
from tests.conftest import Clock, job

pytest.importorskip("moto")

from role_radar.dynamo import REQUEST_STALE_AFTER, DynamoLease, DynamoStateStore  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def lease(table, holder, clock):
    client, name = table
    return DynamoLease(client, name, holder, clock)


def store_for(table, holder_lease, clock):
    client, name = table
    return DynamoStateStore(client, name, holder_lease, clock)


def put_lease_item(table, **attrs):
    """Overwrite the stored lease directly, as another runner could have."""
    client, name = table
    item = {"pk": {"S": "#lease"}, "sk": {"S": "#lease"}}
    for key, value in attrs.items():
        item[key] = {"N": str(value)} if isinstance(value, (int, float)) else {"S": value}
    client.put_item(TableName=name, Item=item)


class CountingClient:
    """Wraps a boto3 client and counts the rows written by transactions."""

    def __init__(self, client):
        self._client = client
        self.transactions: list[list[str]] = []

    def transact_write_items(self, TransactItems):
        self.transactions.append([next(iter(item)) for item in TransactItems])
        return self._client.transact_write_items(TransactItems=TransactItems)

    def __getattr__(self, name):
        return getattr(self._client, name)


def add_jobs(record, *titles):
    state = MonitorState({record.name: record.jobs})
    jobs = [job(t, str(i), company=record.name) for i, t in enumerate(titles)]
    reconcile(state, record.name, jobs, {j.uid: True for j in jobs}, now=T0)
    return jobs


# -- lease ----------------------------------------------------------------------


def test_only_one_runner_can_hold_the_lease(table):
    clock = Clock()
    laptop, lam = lease(table, "laptop:mac", clock), lease(table, "lambda", clock)
    assert laptop.acquire(180)
    assert not lam.acquire(180)
    info = lam.read()
    assert (info.holder, info.epoch) == ("laptop:mac", 1) and info.held(clock())


def test_expired_lease_can_be_taken_and_bumps_the_epoch(table):
    clock = Clock()
    laptop, lam = lease(table, "laptop:mac", clock), lease(table, "lambda", clock)
    laptop.acquire(180)
    clock.advance(181)
    assert lam.acquire(900) and lam.epoch == 2
    assert not laptop.renew(180)  # the laptop finds out it was replaced
    assert laptop.epoch is None and not laptop.held


def test_holder_can_retake_its_own_lease_with_a_new_epoch(table):
    clock = Clock()
    crashed = lease(table, "laptop:mac", clock)
    crashed.acquire(180)
    restarted = lease(table, "laptop:mac", clock)  # same machine, new process
    assert restarted.acquire(180) and restarted.epoch == 2
    assert not crashed.renew(180)  # the old process's epoch is fenced off


def test_release_lets_the_other_side_take_over_immediately(table):
    clock = Clock()
    laptop, lam = lease(table, "laptop:mac", clock), lease(table, "lambda", clock)
    laptop.acquire(180)
    laptop.release()
    assert laptop.epoch is None
    assert lam.acquire(900)
    assert lam.read().released_at is None


def test_check_uses_the_wall_clock_without_io(table):
    clock = Clock()
    laptop = lease(table, "laptop:mac", clock)
    laptop.acquire(180)
    laptop.check()
    clock.advance(180 - laptop.MARGIN)  # e.g. the lid was closed
    with pytest.raises(LeaseLost):
        laptop.check()


def test_verify_notices_a_takeover_before_the_local_expiry(table):
    clock = Clock()
    laptop = lease(table, "laptop:mac", clock)
    laptop.acquire(180)
    put_lease_item(table, holder="lambda", epoch=2, expires_at=clock() + 900)
    laptop.check()  # still looks fine locally
    with pytest.raises(LeaseLost):
        laptop.verify()
    assert laptop.epoch is None


def test_handoff_request_keeps_the_lease_for_the_requester(table):
    clock = Clock()
    lam, laptop, other = lease(table, "lambda", clock), lease(table, "laptop:mac", clock), lease(table, "cli:mac", clock)
    lam.acquire(900)
    assert not laptop.acquire(180)
    laptop.request_handoff()
    assert lam.handoff_requested() == "laptop:mac"
    lam.release()
    assert not other.acquire(180)  # the laptop asked first
    assert laptop.acquire(180)
    assert lam.read().requested_by is None  # cleared on acquire


def test_stale_handoff_request_is_ignored(table):
    clock = Clock()
    lam, laptop = lease(table, "lambda", clock), lease(table, "laptop:mac", clock)
    lam.acquire(60)
    laptop.request_handoff()  # ...and then the laptop goes away
    clock.advance(REQUEST_STALE_AFTER + 1)
    assert lam.handoff_requested() is None
    assert lam.acquire(900)


# -- store --------------------------------------------------------------------


def test_digest_schedule_write_is_fenced_after_takeover(table):
    clock = Clock()
    held = lease(table, "laptop:mac", clock)
    held.acquire(180)
    store = store_for(table, held, clock)
    original = DigestSchedule(next_send_at="2026-09-01T00:30:00Z", interval_minutes=30)
    store.save_digest(original)
    put_lease_item(table, holder="lambda", epoch=2, expires_at=clock() + 900)
    with pytest.raises(LeaseLost):
        store.save_digest(DigestSchedule(next_send_at="2026-09-01T01:00:00Z", interval_minutes=30))
    assert store.load_digest() == original


def test_company_roundtrip_writes_only_changed_rows(table):
    clock = Clock()
    held = lease(table, "laptop:mac", clock)
    held.acquire(180)
    counting = CountingClient(table[0])
    store = DynamoStateStore(counting, table[1], held, clock)

    record = store.load_company("Acme")
    assert record.is_new
    add_jobs(record, "Software Engineer", "Data Engineer")
    record.meta = CompanyMeta(last_checked_at="2026-09-01T00:00:00Z", next_check_at="2026-09-01T00:30:00Z")
    store.save_company(record)
    assert counting.transactions == [["ConditionCheck", "Put", "Put", "Put"]]  # lease check, 2 jobs, schedule

    again = store.load_company("Acme")
    assert again.jobs == record.jobs and again.meta == record.meta and not again.is_new
    assert store.load_schedule() == {"Acme": record.meta}

    again.meta.last_checked_at = "2026-09-01T00:30:00Z"
    store.save_company(again)  # nothing changed but the schedule
    assert counting.transactions[-1] == ["ConditionCheck", "Put"]

    del again.jobs[next(iter(again.jobs))]
    store.save_company(again)
    assert counting.transactions[-1] == ["ConditionCheck", "Delete", "Put"]
    assert len(store.load_company("Acme").jobs) == 1


def test_save_is_fenced_on_the_stored_lease(table):
    clock = Clock()
    held = lease(table, "laptop:mac", clock)
    held.acquire(180)
    store = store_for(table, held, clock)
    record = store.load_company("Acme")
    add_jobs(record, "Software Engineer")

    put_lease_item(table, holder="lambda", epoch=2, expires_at=clock() + 900)  # someone took over
    with pytest.raises(LeaseLost):
        store.save_company(record)
    assert store.load_company("Acme").jobs == {} and store.load_schedule() == {}


def test_save_refuses_locally_once_the_lease_has_expired(table):
    clock = Clock()
    held = lease(table, "laptop:mac", clock)
    held.acquire(180)
    store = store_for(table, held, clock)
    record = store.load_company("Acme")
    add_jobs(record, "Software Engineer")
    clock.advance(3600)  # woke up an hour later
    with pytest.raises(LeaseLost):
        store.save_company(record)
    assert store.load_company("Acme").jobs == {}


def test_big_saves_are_split_with_the_schedule_last(table):
    clock = Clock()
    held = lease(table, "laptop:mac", clock)
    held.acquire(180)
    counting = CountingClient(table[0])
    store = DynamoStateStore(counting, table[1], held, clock)
    record = store.load_company("Megacorp")
    add_jobs(record, *(f"Engineer {i}" for i in range(250)))
    record.meta = CompanyMeta(last_checked_at="2026-09-01T00:00:00Z")
    store.save_company(record)
    assert [len(t) for t in counting.transactions] == [100, 100, 54]  # each led by the lease check
    assert all(t[0] == "ConditionCheck" for t in counting.transactions)
    assert len(store.load_company("Megacorp").jobs) == 250


def test_alert_log_and_run_records(table):
    clock = Clock()
    held = lease(table, "lambda", clock)
    held.acquire(900)
    store = store_for(table, held, clock)
    record = store.load_company("Acme")
    (sent,) = add_jobs(record, "Software Engineer")
    record.jobs[sent.uid].notified_at = "2026-09-01T00:05:00Z"
    record.alerted = [sent.uid]
    store.save_company(record)
    (alert,) = store.recent_alerts()
    assert (alert["company"], alert["title"], alert["by"]) == ("Acme", "Software Engineer", "lambda")
    assert alert["ttl"] > clock()

    store.record_run("lambda", {"finished_at": "2026-09-01T00:06:00Z", "checked": 3, "alerts": 1})
    assert store.last_runs()["lambda"]["checked"] == 3


def test_whole_state_save_and_load_for_migration(table):
    clock = Clock()
    held = lease(table, "migrate:mac", clock)
    held.acquire(600)
    store = store_for(table, held, clock)
    state = MonitorState()
    state.jobs_for("Acme")["acme:x:1"] = SeenJob("Software Engineer", "https://a/1", "fp", "2026-09-01T00:00:00Z", matched=True)
    state.jobs_for("Globex")["globex:x:2"] = SeenJob("Data Engineer", "https://g/2", "fp2", "2026-09-01T00:00:00Z")
    state.meta["Acme"] = CompanyMeta(last_checked_at="2026-09-01T00:00:00Z")
    store.save(state)
    assert store.load().to_dict() == state.to_dict()


def test_a_renewal_cannot_bring_back_a_released_lease(table):
    clock = Clock()
    laptop = lease(table, "laptop:mac", clock)
    laptop.acquire(180)
    epoch = laptop.epoch
    laptop.release()
    laptop.epoch = epoch  # as if a renewal was already in flight when the release happened
    assert not laptop.renew(180)
    assert lease(table, "lambda", clock).acquire(900)  # Lambda isn't locked out


def test_a_stale_lease_call_does_not_forget_a_newer_epoch(table):
    clock = Clock()
    laptop = lease(table, "laptop:mac", clock)
    laptop.acquire(180)
    laptop._forget(laptop.epoch - 1)  # e.g. a renewal from before a re-acquire failing late
    assert laptop.epoch == 1 and laptop.held


def test_migration_stops_when_the_lease_is_taken(table):
    clock = Clock()
    held = lease(table, "migrate:mac", clock)
    held.acquire(180)
    store = store_for(table, held, clock)
    state = MonitorState()
    for name in ("Acme", "Globex"):
        state.jobs_for(name)[f"{name.lower()}:x:1"] = SeenJob("Engineer", "u", "fp", "2026-09-01T00:00:00Z")

    real = store.save_company

    def save_then_lose_the_lease(record):
        real(record)
        put_lease_item(table, holder="lambda", epoch=9, expires_at=clock() + 900)

    store.save_company = save_then_lose_the_lease
    with pytest.raises(LeaseLost):
        store.save(state)
    assert set(DynamoStateStore(*table).load().companies) == {"Acme"}  # nothing written after the loss


def test_runner_switches_round_trip_and_survive_migration(table):
    store = DynamoStateStore(table[0], table[1])
    assert store.load_switches() == {}  # nothing stored: both runners on
    store.save_switch("lambda", False)
    store.save_switch("laptop", False)
    store.save_switch("laptop", True)
    store.save_switch("email", False)
    assert store.load_switches() == {"lambda": False, "laptop": True, "email": False}
    assert store.load().switches == {"lambda": False, "laptop": True, "email": False}
