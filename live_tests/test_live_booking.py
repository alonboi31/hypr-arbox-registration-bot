"""LIVE test against the real Arbox server. Books a REAL class; cancel it by hand afterwards.

Not part of the normal suite (pytest.ini only collects tests/). Run with:
    uv run --env-file .env pytest live_tests -s
"""
import os
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import arbox_client
from app.arbox_client import ArboxClient
from app.opening import opening_time

pytestmark = pytest.mark.skipif(
    not os.getenv("ARBOX_EMAIL") or os.getenv("ARBOX_MOCK") == "1",
    reason="needs real credentials: run with --env-file .env",
)


async def test_books_real_class_closest_to_24h_from_now(monkeypatch):
    # book for real even if .env still says DRY_RUN=1
    monkeypatch.setattr(arbox_client, "settings", replace(arbox_client.settings, dry_run=False))
    client = ArboxClient()
    now = datetime.now(arbox_client.TZ)
    target = now + timedelta(hours=24)

    classes = await client.get_schedule(now.date(), (target + timedelta(days=1)).date())
    candidates = [
        c for c in classes
        if c.start > now and not c.registered and not c.in_standby and not c.full
        and opening_time(c.start, c.registration_hours) <= now  # already bookable
    ]
    assert candidates, "no bookable class found around now+24h"
    ranked = sorted(candidates, key=lambda c: abs(c.start - target))[:5]  # never more than 5 attempts

    chosen = None
    for c in ranked:
        print(f"\nTrying: {c.name} {c.start:%a %d/%m %H:%M} (id {c.id}) "
              f"- {abs(c.start - target)} from the 24h mark")
        try:
            await client.book(c.id)
            chosen = c
            break
        except arbox_client.BookingError as e:
            if "categoryFrequencyRestricts" not in str(e):  # membership limit for that class type: try next
                raise
            print(f"  refused ({e}), trying the next closest")
    assert chosen, "every candidate was refused by the membership frequency limit"

    after = await client.get_class(chosen.id, chosen.start.date())
    assert after is not None and after.registered, "booking request was accepted but class is not registered"
    print(f"Booked and confirmed: {chosen.name} {chosen.start:%a %d/%m %H:%M}. Cancel it by hand in the Arbox app.")
