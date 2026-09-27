"""Handoffs between the laptop and Lambda, against fake AWS (moto).

Both runners share one DynamoDB table (seen jobs, schedule, lease) and one
companies file, as in production. A fake clock drives lease expiry and the
schedule, and the careers sites are canned responses whose handler can pause a
request to interleave the other runner. Throughout, the checks are that every
matching job is alerted exactly once, no company is skipped, and only the
lease holder scrapes, saves or sends.
"""

import asyncio
import random
import threading
from collections import Counter

import httpx
import pytest

from role_radar import monitor, runner as runner_module, schedule
from role_radar.backends import Backend, ConfigSource
from role_radar.config import RuntimeSettings
from role_radar.http_client import HttpClient
from role_radar.monitor import EXIT_LEASE_LOST
from role_radar.runner import Runner
from tests.conftest import Clock
from tests.test_monitor import RecordingNotifier

pytest.importorskip("moto")

from role_radar.dynamo import DynamoLease, DynamoStateStore  # noqa: E402

MATCHING = ("Software Developer", "Data Engineer")


def listing(index: int, extra: tuple[str, ...] = ()) -> list[dict]:
    titles = [*MATCHING, "Recruiter", *extra]
    return [{"id": f"{index}{n:02d}", "jobOpeningName": title} for n, title in enumerate(titles)]


class World:
    """One shared table and config, fake careers sites, and runners that use them."""

    def __init__(self, table, tmp_path, monkeypatch, companies: int = 6) -> None:
        self.client, self.table = table
        self.clock = Clock()
        self.sites = {f"c{i}": listing(i) for i in range(companies)}
        self.scrapes: list[str] = []
        self.hook = None  # async fn(site) run inside each listing request, to interleave the other runner
        self.config_path = tmp_path / "companies.yaml"
        self.config_path.write_text(
            "settings:\n"
            "  max_company_concurrency: 1  # one company at a time, so a test can pause mid-pass\n"
            "  http: {per_domain_delay: 0, respect_robots: false, max_retries: 0}\n"
            "defaults:\n"
            "  filters: {include_keywords: [software developer, data engineer]}\n"
            "companies:\n" + "".join(f"  - {{name: C{i}, url: https://c{i}.bamboohr.com/careers}}\n" for i in range(companies))
        )

        async def handler(request):
            site = request.url.host.split(".")[0]
            self.scrapes.append(site)
            if self.hook:
                await self.hook(site)
            return httpx.Response(200, json={"result": self.sites[site]})

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(monitor, "HttpClient", lambda s, **kw: HttpClient(s, transport=transport, **kw))

    def runner(self, holder: str, name: str | None = None, clock=None) -> tuple[Runner, RecordingNotifier]:
        clock = clock or self.clock
        lease = DynamoLease(self.client, self.table, holder, clock)
        store = DynamoStateStore(self.client, self.table, lease, clock)
        notifier = RecordingNotifier()
        source = ConfigSource(RuntimeSettings(), self.config_path)
        return Runner(source, Backend(store, lease), [notifier], clock=clock, name=name), notifier

    def lease(self):
        return DynamoLease(self.client, self.table, "observer").read()

    def store(self) -> DynamoStateStore:
        return DynamoStateStore(self.client, self.table)


def alerted(*notifiers: RecordingNotifier) -> Counter:
    """uid → times alerted, across runners."""
    return Counter(job.uid for n in notifiers for batch in n.batches for job in batch)


def all_matching(companies: int = 6) -> set[str]:
    return {f"c{i}:bamboohr:{i}{n:02d}" for i in range(companies) for n in range(len(MATCHING))}


def uids(counter: Counter) -> set[str]:
    return {uid.replace("C", "c", 1) for uid in counter}


async def until(condition, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def world(table, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "HANDOFF_POLL", 0)  # Lambda looks for a handoff request before every company
    monkeypatch.setattr(runner_module, "WAIT_POLL", 0.05)  # a waiting laptop retries at once
    monkeypatch.setattr(schedule, "_rng", random.Random(7))
    return World(table, tmp_path, monkeypatch)


def test_laptop_started_while_lambda_works_takes_over_from_it(world):
    lam, lam_alerts = world.runner("lambda", name="lambda")
    laptop, laptop_alerts = world.runner("laptop:mac")
    stop = asyncio.Event()

    async def go():
        laptop_task = None

        async def laptop_starts_during_c1(site):
            nonlocal laptop_task
            if site == "c1" and laptop_task is None:
                laptop_task = asyncio.create_task(laptop.serve(stop))  # you start the app now
                await until(lambda: world.lease().requested_by == "laptop:mac")

        world.hook = laptop_starts_during_c1
        result = await lam.lambda_pass(840, holder="lambda:run1")
        world.hook = None
        # Lambda finished the company in flight, started no more, and released.
        assert [o.company for o in result.checked] == ["C0", "C1"]
        assert len(result.outcomes) == 6  # C2..C5 were left for the laptop
        assert lam.lease.epoch is None  # released
        await until(lambda: len(alerted(laptop_alerts)) == 8)  # the laptop checked C2..C5
        assert world.lease().holder == "laptop:mac" and world.lease().held(world.clock())
        stop.set()
        await laptop_task

    asyncio.run(go())
    both = alerted(lam_alerts, laptop_alerts)
    assert uids(both) == all_matching() and set(both.values()) == {1}  # everything once, nothing twice
    assert Counter(world.scrapes) == {f"c{i}": 1 for i in range(6)}  # each company checked once
    assert world.lease().released_at  # quitting released it for Lambda


def test_quitting_the_laptop_lets_lambda_carry_on(world):
    laptop, laptop_alerts = world.runner("laptop:mac")
    lam, lam_alerts = world.runner("lambda", name="lambda")
    stop = asyncio.Event()

    async def laptop_session():
        task = asyncio.create_task(laptop.serve(stop))
        await until(lambda: len(alerted(laptop_alerts)) == 12)
        stop.set()  # Ctrl+C
        await task

    asyncio.run(laptop_session())
    assert world.lease().released_at

    world.sites["c3"] = listing(3, extra=("Backend Developer, Software Developer II",))  # one new match
    world.clock.advance(3600)  # an hour later every company is due again
    result = asyncio.run(lam.lambda_pass(840, holder="lambda:run1"))
    assert result is not None and len(result.checked) == 6  # took over at once: the lease was released
    (new,) = alerted(lam_alerts)
    assert new == "c3:bamboohr:303" and set(alerted(laptop_alerts).values()) == {1}


def test_closing_the_lid_hands_over_once_the_lease_expires(world):
    laptop, laptop_alerts = world.runner("laptop:mac")
    lam, lam_alerts = world.runner("lambda", name="lambda")

    async def laptop_pass_then_lid_closes():
        await laptop._take_lease()
        await laptop.pass_once()  # ...and then the process is frozen: no renewals, no release

    asyncio.run(laptop_pass_then_lid_closes())
    scraped = len(world.scrapes)
    world.clock.advance(60)
    assert asyncio.run(lam.lambda_pass(840, holder="lambda:run1")) is None  # still the laptop's
    assert len(world.scrapes) == scraped

    world.clock.advance(3600)  # long after the lease expired; companies are due again
    world.sites["c0"] = listing(0, extra=("Software Developer (Payments)",))
    result = asyncio.run(lam.lambda_pass(840, holder="lambda:run2"))
    assert result is not None and len(result.checked) == 6 and lam.lease.epoch is None  # released at the end
    assert set(alerted(lam_alerts)) == {"c0:bamboohr:003"}
    assert set(alerted(laptop_alerts).values()) == {1}


def test_lease_expiring_mid_pass_stops_the_laptop_before_it_saves_or_alerts(world):
    laptop, laptop_alerts = world.runner("laptop:mac")
    lam, lam_alerts = world.runner("lambda", name="lambda")

    async def go():
        async def lid_closes_during_c1(site):
            if site == "c1" and not world.lease().holder.startswith("lambda"):
                world.clock.advance(200)  # asleep past the 3-minute lease...
                assert await lam.lambda_pass(840, holder="lambda:run1")  # ...so Lambda takes over meanwhile

        await laptop._take_lease()
        world.hook = lid_closes_during_c1
        return await laptop.pass_once()

    result = asyncio.run(go())
    assert result.lease_lost and result.exit_code == EXIT_LEASE_LOST
    assert uids(alerted(laptop_alerts)) == {"c0:bamboohr:000", "c0:bamboohr:001"}  # only before the pause
    both = alerted(lam_alerts, laptop_alerts)
    assert uids(both) == all_matching() and set(both.values()) == {1}
    c1_alerts = [a for a in world.store().recent_alerts(20) if a["company"] == "C1"]
    assert len(c1_alerts) == 2 and all(a["by"] == "lambda:run1" for a in c1_alerts)  # C1 saved by Lambda only


def test_laptop_with_a_slow_clock_is_stopped_by_the_stored_lease(world):
    """Clock skew: the laptop still thinks its lease is valid, so only the store can stop it."""
    laptop, laptop_alerts = world.runner("laptop:mac")
    lam, lam_alerts = world.runner("lambda", name="lambda", clock=lambda: world.clock() + 400)

    async def go():
        async def lambda_takes_over_during_c1(site):
            if site == "c1" and world.lease().holder == "laptop:mac":
                assert await lam.lambda_pass(840, holder="lambda:run1")

        await laptop._take_lease()
        world.hook = lambda_takes_over_during_c1
        return await laptop.pass_once()

    result = asyncio.run(go())
    assert result.lease_lost  # verify() before alerting saw Lambda's epoch
    assert uids(alerted(laptop_alerts)) == {"c0:bamboohr:000", "c0:bamboohr:001"}
    both = alerted(lam_alerts, laptop_alerts)
    assert uids(both) == all_matching() and set(both.values()) == {1}


def test_stale_save_is_rejected_by_the_transaction_fence(world):
    """Nothing new to alert, so the save itself must be refused."""
    laptop, _ = world.runner("laptop:mac")
    lam, _ = world.runner("lambda", name="lambda", clock=lambda: world.clock() + 400)

    async def first_round():
        await laptop._take_lease()
        await laptop.pass_once()
        await asyncio.to_thread(laptop.lease.release)

    asyncio.run(first_round())
    world.clock.advance(3600)
    lambda_checked_c1 = []

    async def second_round():
        async def lambda_takes_over_during_c1(site):
            if site == "c1" and world.lease().holder == "laptop:mac":
                await lam.lambda_pass(840, holder="lambda:run1")
                lambda_checked_c1.append(world.store().load_schedule()["C1"].last_checked_at)

        await laptop._take_lease()
        world.hook = lambda_takes_over_during_c1
        return await laptop.pass_once()

    result = asyncio.run(second_round())
    assert result.lease_lost
    # C1's schedule is still the one Lambda saved: the laptop's stale save never landed.
    assert world.store().load_schedule()["C1"].last_checked_at == lambda_checked_c1[0]


class AtomicRequests:
    """DynamoDB applies each conditional write atomically. moto doesn't lock between
    threads, so this serializes its requests; the threads still race for the order."""

    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()

    def __getattr__(self, name):
        method = getattr(self._client, name)

        def call(*args, **kwargs):
            with self._lock:
                return method(*args, **kwargs)

        return call


def test_only_one_of_several_runners_racing_for_the_lease_wins(world):
    client = AtomicRequests(world.client)
    for _ in range(10):
        world.client.delete_item(TableName=world.table, Key={"pk": {"S": "#lease"}, "sk": {"S": "#lease"}})
        racers = [DynamoLease(client, world.table, f"runner-{i}", world.clock) for i in range(8)]
        barrier = threading.Barrier(len(racers))
        winners = []

        def race(lease):
            barrier.wait()
            if lease.acquire(180):
                winners.append(lease.holder)

        threads = [threading.Thread(target=race, args=(lease,)) for lease in racers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1 and world.lease().holder == winners[0]


def test_lambda_does_no_scraping_while_the_laptop_holds_the_lease(world):
    laptop, _ = world.runner("laptop:mac")
    lam, lam_alerts = world.runner("lambda", name="lambda")
    asyncio.run(laptop._take_lease())
    for _ in range(3):  # three 5-minute ticks, with the laptop renewing every minute
        for _ in range(5):
            world.clock.advance(60)
            assert laptop.lease.renew(180)
        assert asyncio.run(lam.lambda_pass(840, holder="lambda:tick")) is None
    assert world.scrapes == [] and lam_alerts.batches == []
    assert "lambda" not in world.store().last_runs()
    assert world.lease().holder == "laptop:mac" and world.lease().epoch == 1


def test_lambda_every_five_minutes_checks_each_company_about_every_half_hour(table, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "_rng", random.Random(7))
    world = World(table, tmp_path, monkeypatch, companies=12)
    lam, _ = world.runner("lambda", name="lambda")
    per_run = []
    for tick in range(24):  # two hours
        world.clock.advance(300)
        before = len(world.scrapes)
        asyncio.run(lam.lambda_pass(840, holder=f"lambda:{tick}"))
        per_run.append(len(world.scrapes) - before)

    counts = Counter(world.scrapes)
    assert all(3 <= counts[f"c{i}"] <= 4 for i in range(12)), counts  # not 24 each
    assert sum(counts.values()) <= 48 < 24 * 12
    assert per_run[0] == 12  # the first run checks everything...
    assert sum(1 for n in per_run[1:13] if n) >= 3  # ...then the next round is spread across the half hour
