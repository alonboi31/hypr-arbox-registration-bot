from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app import arbox_client
from app.arbox_client import ArboxClient, BookingError, NotOpenYet
from tests.fake_arbox import MEMBERSHIP_PLAN, TZ, FakeArbox, raw_class

DAY = datetime(2026, 10, 21, 18, 0, tzinfo=TZ)


@pytest.fixture
def fake():
    f = FakeArbox()
    f.add(raw_class(1, DAY, hours=48, booked=5))
    f.add(raw_class(2, DAY.replace(hour=19), booked=20, category="HYROX"))          # full
    f.add(raw_class(3, DAY + timedelta(days=1), hours=72, user_booked=555))          # next day, mine
    return f


@pytest.fixture
def client(fake):
    return ArboxClient(transport=fake.transport)


async def test_login_sets_both_token_headers(client, fake):
    await client.login()
    assert client._tokens == {"accesstoken": "tok-1", "refreshtoken": "ref-1"}
    assert fake.logins == 1


async def test_bad_credentials_raise(fake, monkeypatch):
    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, arbox_password="wrong"))
    with pytest.raises(Exception):
        await ArboxClient(transport=fake.transport).login()


async def test_schedule_is_queried_one_day_at_a_time_and_parsed(client, fake):
    classes = await client.get_schedule(date(2026, 10, 21), date(2026, 10, 22))
    assert [p for m, p, b in fake.calls if p.endswith("betweenDates")] == ["/api/v2/schedule/betweenDates"] * 2
    by_id = {c.id: c for c in classes}
    assert set(by_id) == {"1", "2", "3"}
    assert by_id["1"].name == "W.O.D Hall A" and by_id["1"].start == DAY and not by_id["1"].full
    assert by_id["1"].registration_hours == 48 and by_id["1"].coach == "Coach C"
    assert by_id["2"].full and by_id["2"].name == "HYROX"
    assert by_id["3"].registered and by_id["3"].registration_hours == 72


async def test_expired_token_triggers_one_relogin_then_succeeds(client, fake):
    await client.login()
    fake.reject_token_once = True
    classes = await client.get_schedule(date(2026, 10, 21), date(2026, 10, 21))
    assert len(classes) == 2
    assert fake.logins == 2


async def test_membership_prefers_active_plan_over_session_pack(client):
    assert await client.membership_id() == MEMBERSHIP_PLAN


async def test_membership_can_be_pinned(fake, monkeypatch):
    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, membership_user_id=42))
    c = ArboxClient(transport=fake.transport)
    assert await c.membership_id() == 42
    assert not [p for m, p, b in fake.calls if "memberships" in p]


async def test_no_active_membership_is_an_error(client, fake):
    fake.memberships = [{"id": 1, "active": 0, "mt_type": "plan"}]
    with pytest.raises(BookingError):
        await client.membership_id()


async def test_book_success_sends_exact_payload(client, fake):
    await client.book("1")
    assert fake.posts("/scheduleUser/insert") == [
        {"schedule_id": 1, "membership_user_id": MEMBERSHIP_PLAN, "extras": {"spot": None, "invited_by": None}}
    ]
    assert fake.booked == [1]


async def test_book_before_opening_raises_not_open_yet(client, fake):
    fake.open_at = datetime.now(TZ) + timedelta(hours=1)
    with pytest.raises(NotOpenYet):
        await client.book("1")


async def test_book_refusal_raises_booking_error_with_reason(client, fake):
    with pytest.raises(BookingError, match="scheduleIsFull"):
        await client.book("2")


async def test_server_error_is_treated_as_retryable(client, fake):
    fake.fail_next = 1
    with pytest.raises(NotOpenYet):
        await client.book("1")
    await client.book("1")  # next call works
    assert fake.booked == [1]


async def test_waitlist_uses_standby_endpoint(client, fake):
    await client.join_waitlist("2")
    assert len(fake.posts("/scheduleStandBy/insert")) == 1 and fake.standby == [2]
    assert not fake.posts("/scheduleUser/insert")


async def test_get_class_returns_fresh_state(client, fake):
    assert (await client.get_class("2", DAY.date())).full
    assert await client.get_class("999", DAY.date()) is None


async def test_dry_run_sends_nothing(fake, monkeypatch):
    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, dry_run=True))
    c = ArboxClient(transport=fake.transport)
    await c.book("1")
    await c.join_waitlist("2")
    assert not fake.posts("/scheduleUser/insert") and not fake.posts("/scheduleStandBy/insert")
