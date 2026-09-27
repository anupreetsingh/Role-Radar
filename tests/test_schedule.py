import random
from datetime import datetime, timedelta, timezone

from role_radar.schedule import after_check, due_at, due_companies, next_due
from role_radar.storage import CompanyMeta, from_iso, to_iso
from tests.conftest import company

T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
INTERVAL = timedelta(minutes=30)


def meta_due(at: datetime) -> CompanyMeta:
    return CompanyMeta(last_checked_at=to_iso(at - INTERVAL), next_check_at=to_iso(at))


def test_due_companies_never_checked_first_then_most_overdue():
    companies = [company("A"), company("B"), company("C"), company("D"), company("E")]
    schedule = {
        "A": meta_due(T0 - timedelta(minutes=1)),
        "B": meta_due(T0 + timedelta(minutes=1)),  # not due yet
        "D": meta_due(T0 - timedelta(minutes=10)),
        "E": meta_due(T0),  # due exactly now
    }
    assert [c.name for c in due_companies(companies, schedule, T0, INTERVAL)] == ["C", "D", "A", "E"]


def test_migrated_company_without_next_check_is_due_an_interval_after_its_last_check():
    meta = CompanyMeta(last_checked_at=to_iso(T0))
    assert due_at(meta, INTERVAL) == T0 + INTERVAL
    assert due_at(None, INTERVAL) is None


def test_on_time_check_is_next_due_exactly_one_interval_later():
    meta = after_check(meta_due(T0), T0 + timedelta(minutes=2), INTERVAL)
    assert from_iso(meta.next_check_at) == T0 + timedelta(minutes=32)
    assert (meta.failures, meta.last_error, meta.last_checked_at) == (0, None, to_iso(T0 + timedelta(minutes=2)))


def test_first_and_late_checks_spread_the_next_check_over_an_interval():
    firsts = {from_iso(after_check(CompanyMeta(), T0, INTERVAL, rng=random.Random(seed)).next_check_at) for seed in range(20)}
    assert all(T0 + INTERVAL <= t < T0 + 2 * INTERVAL for t in firsts)
    assert max(firsts) - min(firsts) > INTERVAL / 2  # actually spread out
    late = after_check(meta_due(T0 - timedelta(hours=3)), T0, INTERVAL, rng=random.Random(3))
    assert T0 + INTERVAL < from_iso(late.next_check_at) < T0 + 2 * INTERVAL


def test_failures_back_off_up_to_8x_and_success_resets():
    meta, at, delays = meta_due(T0), T0, []
    for _ in range(7):
        meta = after_check(meta, at, INTERVAL, error="HTTP 500")
        nxt = from_iso(meta.next_check_at)
        delays.append((nxt - at) / INTERVAL)
        at = nxt
    assert delays == [1, 1, 2, 4, 8, 8, 8]
    assert meta.failures == 7 and meta.last_error == "HTTP 500"
    meta = after_check(meta, at, INTERVAL)
    assert meta.failures == 0 and meta.last_error is None
    assert from_iso(meta.next_check_at) - at == INTERVAL


def test_next_due():
    companies = [company("A"), company("B")]
    schedule = {"A": meta_due(T0 + timedelta(minutes=20)), "B": meta_due(T0 + timedelta(minutes=5))}
    assert next_due(companies, schedule, T0, INTERVAL) == T0 + timedelta(minutes=5)
    assert next_due(companies, {"A": schedule["A"]}, T0, INTERVAL) == T0  # B never checked: due now
    assert next_due([], schedule, T0, INTERVAL) is None
