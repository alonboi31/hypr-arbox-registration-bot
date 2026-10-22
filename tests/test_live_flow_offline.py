"""The live test's logic, run against the fake Arbox so it is verified without touching a real account."""
from datetime import datetime, timedelta

import pytest

from app import db, engine
from live_tests.flow import book_next_available_and_cancel
from tests.fake_arbox import TZ, raw_class
from tests.test_engine import client, fake, fast  # noqa: F401  (fixtures)


def at(hours, minute=0):
    return (datetime.now(TZ) + timedelta(hours=hours)).replace(minute=minute, second=0, microsecond=0)


async def test_books_the_soonest_class_that_is_far_enough_away_then_cancels(client, fake, fast):  # noqa: F811
    fake.add(raw_class(1, at(2), category="TOO SOON"))        # inside the 4h safety margin
    fake.add(raw_class(2, at(30), category="LATER"))
    fake.add(raw_class(3, at(6), category="NEXT ONE"))        # the soonest eligible
    fake.add(raw_class(4, at(8), category="FULL", booked=20))
    booked = await book_next_available_and_cancel(client, say=lambda m: None)
    assert booked.id == "3"
    assert fake.booked == []  # cancelled again
    assert db.get_attempt("3")["status"] == "booked"
    assert len(fake.posts("/scheduleUser/delete")) == 1


async def test_skips_a_class_type_the_membership_limit_refuses(client, fake, fast):  # noqa: F811
    fake.add(raw_class(10, at(6), category="WOD"))
    fake.add(raw_class(11, at(7), category="MOBILITY"))
    fake.restricted = {"WOD"}
    booked = await book_next_available_and_cancel(client, say=lambda m: None)
    assert booked.id == "11" and fake.booked == []


async def test_fails_loudly_when_every_class_is_refused(client, fake, fast):  # noqa: F811
    fake.add(raw_class(20, at(6), category="WOD"))
    fake.restricted = {"WOD"}
    with pytest.raises(AssertionError, match="frequency limit"):
        await book_next_available_and_cancel(client, say=lambda m: None)


async def test_unexpected_failure_is_not_hidden(client, fake, fast, monkeypatch):  # noqa: F811
    fake.add(raw_class(30, at(6)))
    fake.fail_next = 10_000  # the server errors on every booking
    monkeypatch.setattr(engine, "RETRY_WINDOW", timedelta(seconds=0.3))
    with pytest.raises(AssertionError, match="unexpected failure"):
        await book_next_available_and_cancel(client, say=lambda m: None)
