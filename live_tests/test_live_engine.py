"""LIVE test against the real Arbox: the bot's own engine books the next available class, then it is cancelled.

Uses a temporary database (your real rules and history are not touched) and no emails or phone alerts.
Run with:
    uv run --env-file .env pytest live_tests/test_live_engine.py -s
"""
import os
from dataclasses import replace

import pytest

from app import arbox_client, calendar_invite, calendar_sync, db, engine
from live_tests.flow import book_next_available_and_cancel

pytestmark = pytest.mark.skipif(
    not os.getenv("ARBOX_EMAIL") or os.getenv("ARBOX_MOCK") == "1",
    reason="needs real credentials: run with --env-file .env",
)


async def test_engine_books_the_next_available_class_then_cancels(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "settings", replace(db.settings, db_path=str(tmp_path / "live.db")))
    db.init()
    for mod in (arbox_client, engine):  # book for real even if .env says DRY_RUN=1
        monkeypatch.setattr(mod, "settings", replace(mod.settings, dry_run=False))
    for mod in (calendar_invite, calendar_sync):  # no calendar emails from a test
        monkeypatch.setattr(mod, "settings", replace(mod.settings, calendar_to=""))

    async def quiet(msg):  # no ntfy alert either
        print("  (notification)", msg)

    monkeypatch.setattr(engine, "notify", quiet)
    await book_next_available_and_cancel(arbox_client.ArboxClient())
