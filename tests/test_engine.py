"""Engine integration tests: real engine + real ArboxClient + fake Arbox server + real SQLite."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest

from app import db, engine
from app.arbox_client import ArboxClient, GymClass
from tests.fake_arbox import TZ, FakeArbox, raw_class


def future_start(days=3, hour=18):
    return (datetime.now(TZ) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def fake():
    return FakeArbox()


@pytest.fixture
def client(fake):
    return ArboxClient(transport=fake.transport)


@pytest.fixture
def fast(monkeypatch, fake):
    """Short retry timings; engine's notion of 'opens at' follows fake.open_at."""
    monkeypatch.setattr(engine, "RETRY_EVERY", 0.02)
    monkeypatch.setattr(engine, "RETRY_WINDOW", timedelta(seconds=1))
    notes = []

    async def fake_notify(msg):
        notes.append(msg)

    monkeypatch.setattr(engine, "notify", fake_notify)
    return notes


def opens_in(monkeypatch, fake, seconds, server_seconds=None):
    """Make the engine think registration opens in `seconds` (server may disagree)."""
    now = datetime.now(TZ)
    fake.open_at = now + timedelta(seconds=seconds if server_seconds is None else server_seconds)
    engine_open = now + timedelta(seconds=seconds)
    monkeypatch.setattr(engine, "opening_time", lambda start, hours=None: engine_open)


async def run_tick(client):
    tasks: set = set()
    await engine.tick(client, tasks)
    started = len(tasks)  # finished tasks remove themselves, so count first
    if tasks:
        await asyncio.gather(*tasks)
    return started


def rule_for(start, name=""):
    db.add_rule(start.weekday(), f"{start:%H:%M}", name)


def status_of(cid):
    a = db.get_attempt(str(cid))
    return a and a["status"]


async def test_books_matching_class_at_opening(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(10, start, booked=4))
    rule_for(start, "hall a")  # case-insensitive substring of "W.O.D Hall A"
    opens_in(monkeypatch, fake, 0.2)

    assert await run_tick(client) == 1
    assert fake.booked == [10]
    assert status_of(10) == "booked"
    assert any("Booked" in n for n in fast)


async def test_retries_until_server_opens(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(11, start))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1, server_seconds=0.5)  # server opens later than engine expects

    await run_tick(client)
    assert fake.booked == [11] and status_of(11) == "booked"
    assert len(fake.posts("/scheduleUser/insert")) >= 2  # got "not open yet" at least once


async def test_second_tick_does_not_double_book(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(12, start))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)

    assert await run_tick(client) == 1
    assert await run_tick(client) == 0  # already registered / claimed
    assert fake.booked == [12]  # booked exactly once (early 'not open yet' attempts don't count)


async def test_non_matching_classes_are_ignored(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(20, start))                                    # no rule for this slot
    fake.add(raw_class(21, start.replace(hour=19)))                   # different time
    fake.add(raw_class(22, start, category="HYROX"))                  # wrong name
    fake.add(raw_class(23, start, user_booked=5))                     # I'm already in
    fake.add(raw_class(24, start, user_in_standby=5))                 # already on waitlist
    db.add_rule(start.weekday(), f"{start:%H:%M}", "wod")             # matches none of the above
    db.add_rule((start.weekday() + 1) % 7, "18:00", "")               # other weekday
    opens_in(monkeypatch, fake, 0.1)

    assert await run_tick(client) == 0
    assert not fake.booked and not fake.posts("/scheduleUser/insert")


async def test_disabled_rule_does_nothing(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(30, start))
    rule_for(start)
    db.set_rule_enabled(db.list_rules()[0]["id"], False)
    opens_in(monkeypatch, fake, 0.1)
    assert await run_tick(client) == 0
    assert not fake.calls or not fake.posts("/schedule/betweenDates")  # didn't even fetch the schedule


async def test_class_far_from_opening_is_not_claimed_yet(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(40, start))
    rule_for(start)
    opens_in(monkeypatch, fake, 3 * 3600)  # hours away, beyond the next tick
    assert await run_tick(client) == 0
    assert db.get_attempt("40") is None  # untouched, will be picked up on a later tick


async def test_full_class_goes_to_waitlist(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(50, start, booked=20))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)

    await run_tick(client)
    assert not fake.booked and fake.standby == [50]
    assert status_of(50) == "waitlisted"
    assert any("waitlist" in n for n in fast)


async def test_other_refusal_is_reported_as_failure(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(60, start, booked=3))
    fake.booked.append(60)  # server will answer "alreadyRegistered" though schedule says not booked
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)

    await run_tick(client)
    a = db.get_attempt("60")
    assert a["status"] == "failed" and "alreadyRegistered" in a["detail"]
    assert any("FAILED" in n for n in fast)
    assert not fake.standby


async def test_refusal_but_actually_registered_counts_as_booked(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(61, start, booked=3, user_booked=999))  # e.g. our earlier request succeeded
    fake.booked.append(61)
    c = GymClass("61", "W.O.D Hall A", start, registration_hours=48)
    db.claim_attempt("61", "x")
    monkeypatch.setattr(engine, "opening_time", lambda s, h=None: datetime.now(TZ) - timedelta(seconds=1))
    await engine.book_when_open(client, c)
    assert status_of(61) == "booked"


async def test_gives_up_after_retry_window(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(70, start))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.05, server_seconds=3600)  # server never opens in time

    await run_tick(client)
    a = db.get_attempt("70")
    assert a["status"] == "failed" and "registerScheduleDisabled" in a["detail"]
    assert any("FAILED" in n for n in fast)


async def test_waitlist_crash_is_recorded_not_lost(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(80, start, booked=20))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)

    async def boom(_):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(client, "join_waitlist", boom)
    await run_tick(client)
    assert status_of(80) == "failed"  # not stuck on 'pending'


async def test_lookup_crash_after_refusal_is_recorded(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(81, start, booked=20))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)

    async def boom(*a):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(client, "get_class", boom)
    await run_tick(client)
    assert status_of(81) == "failed"


async def test_tick_survives_schedule_errors_in_run_forever(client, fake, monkeypatch):
    calls = []

    async def bad_tick(c, tasks):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "tick", bad_tick)
    monkeypatch.setattr(engine, "TICK_SECONDS", 0.01)
    t = asyncio.create_task(engine.run_forever(client))
    await asyncio.sleep(0.1)
    t.cancel()
    assert len(calls) >= 3  # kept looping after errors


async def test_notify_posts_to_ntfy_when_configured(monkeypatch):
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.content))
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(engine.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(engine, "settings", replace(engine.settings, ntfy_topic="my-topic"))
    await engine.notify("Booked: WOD")
    assert seen == [("https://ntfy.sh/my-topic", b"Booked: WOD")]


async def test_notify_failure_never_raises(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(engine.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(engine, "settings", replace(engine.settings, ntfy_topic="my-topic"))
    await engine.notify("x")  # must not raise


async def test_notify_skips_network_without_topic(monkeypatch):
    def handler(req):
        raise AssertionError("should not be called")

    real = httpx.AsyncClient
    monkeypatch.setattr(engine.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await engine.notify("x")


async def test_dry_run_rehearses_without_booking_then_goes_live(fake, fast, monkeypatch):
    from app import arbox_client
    start = future_start()
    fake.add(raw_class(90, start, booked=3))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.1)
    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, dry_run=True))
    monkeypatch.setattr(engine, "settings", replace(engine.settings, dry_run=True))
    client = ArboxClient(transport=fake.transport)

    await run_tick(client)
    assert status_of(90) == "dry-run" and not fake.booked
    assert any("DRY RUN" in n for n in fast) and not any(n.startswith("Booked") for n in fast)
    assert await run_tick(client) == 0  # rehearsal is not repeated every tick

    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, dry_run=False))
    monkeypatch.setattr(engine, "settings", replace(engine.settings, dry_run=False))
    await run_tick(client)  # live now: the rehearsed class is booked for real
    assert fake.booked == [90] and status_of(90) == "booked"


def test_retry_delay_backs_off():
    assert engine.retry_delay(0) == engine.RETRY_EVERY
    assert engine.retry_delay(10) == 1.0
    assert engine.retry_delay(90) == 2.0


async def test_preflight_refreshes_schedule_before_booking(client, fake, fast, monkeypatch):
    start = future_start()
    fake.add(raw_class(13, start))
    rule_for(start)
    opens_in(monkeypatch, fake, 0.2)
    await run_tick(client)
    paths = [p for _, p, _ in fake.calls]
    sched = [i for i, p in enumerate(paths) if p.endswith("betweenDates")]
    first_post = next(i for i, p in enumerate(paths) if p.endswith("/scheduleUser/insert"))
    # tick fetch(es) + one pre-flight refresh, all before the first booking POST
    assert len(sched) >= 2 and sched[-1] < first_post


async def test_poke_triggers_immediate_tick(client, monkeypatch):
    calls = []

    async def counting_tick(c, tasks):
        calls.append(1)

    monkeypatch.setattr(engine, "tick", counting_tick)  # TICK_SECONDS stays at 1 hour
    t = asyncio.create_task(engine.run_forever(client))
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    engine.poke()  # a rule changed
    await asyncio.sleep(0.05)
    t.cancel()
    assert len(calls) == 2
