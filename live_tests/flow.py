"""The whole bot pipeline in one go: a rule books the next available class, then it is cancelled again.

Shared by the live test (real Arbox) and an offline test (fake Arbox) so the logic is checked without
touching a real account.
"""
import asyncio
from datetime import datetime, timedelta

from app import db, engine
from app.opening import opening_time

MIN_LEAD = timedelta(hours=4)  # stay clear of Arbox's late-cancellation cutoff so the cancel is free


async def book_next_available_and_cancel(client, say=print, max_tries: int = 5):
    now = datetime.now(engine.TZ)
    classes = await client.get_schedule(now.date(), (now + timedelta(days=3)).date())
    candidates = sorted(
        (c for c in classes
         if c.start >= now + MIN_LEAD and not c.registered and not c.in_standby and not c.full
         and opening_time(c.start, c.registration_hours) <= now),
        key=lambda c: c.start,
    )[:max_tries]
    assert candidates, "no bookable class found in the next 3 days"

    booked = None
    try:
        for c in candidates:
            say(f"\nTrying: {c.name} {c.start:%a %d/%m %H:%M} (id {c.id})")
            for r in db.list_rules():  # one rule at a time
                db.set_rule_enabled(r["id"], False)
            db.add_rule(c.start.weekday(), f"{c.start:%H:%M}", c.name, f"{c.start:%Y-%m-%d}")  # a one-time rule
            tasks: set = set()
            await engine.tick(client, tasks)  # the real engine: claim, wait for opening, book, verify
            await asyncio.gather(*tasks)
            attempt = db.get_attempt(c.id)
            assert attempt, "the engine never picked the class up"
            if attempt["status"] == "booked":
                booked = c
                break
            detail = attempt["detail"] or ""
            say(f"  not booked: {attempt['status']} {detail}")
            assert "categoryFrequencyRestricts" in detail, f"unexpected failure: {attempt}"  # a membership limit: next
        assert booked, "every candidate was refused by the membership frequency limit"

        mine = await client.get_class(booked.id, booked.start.date())
        assert mine and mine.registered, "the engine says booked, but Arbox does not show me registered"
        once = [r for r in db.list_rules() if r["once_date"] == f"{booked.start:%Y-%m-%d}" and r["time"] == f"{booked.start:%H:%M}"]
        assert once and once[0]["enabled"] == 0, "the one-time rule should switch itself off after booking"
        say(f"Booked and confirmed: {booked.name} {booked.start:%a %d/%m %H:%M}")
    finally:
        if booked:  # always undo what this test booked
            mine = await client.get_class(booked.id, booked.start.date())
            if mine and mine.registered:
                try:
                    await client.cancel(mine)
                except Exception as e:
                    say(f"!!! COULD NOT CANCEL {booked.name} {booked.start:%a %d/%m %H:%M}: {e}. Cancel it by hand in the Arbox app.")
                    raise
            gone = await client.get_class(booked.id, booked.start.date())
            assert gone is not None and not gone.registered, "cancel was sent but the class is still registered"
            say("Cancelled again and confirmed.")
    return booked
