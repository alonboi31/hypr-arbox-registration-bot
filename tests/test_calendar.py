"""Calendar invites: .ics content, SMTP sending, and the engine/API hooks."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import calendar_invite as ci
from app import db, engine
from app.arbox_client import ArboxClient, GymClass
from tests.fake_arbox import TZ, FakeArbox, raw_class
from tests.test_engine import client, fake, fast, future_start, opens_in, run_tick  # noqa: F401  (fixtures)

CFG = dict(calendar_to="me@example.com", smtp_host="smtp.test", smtp_port=587,
           smtp_user="bot@example.com", smtp_password="app-pw", smtp_from="")


@pytest.fixture
def cal(monkeypatch):
    """Calendar enabled in every module that reads settings; SMTP captured instead of sent."""
    for mod in (ci, engine):
        monkeypatch.setattr(mod, "settings", replace(mod.settings, **CFG))
    sent = []
    monkeypatch.setattr(ci, "_send", lambda msg: sent.append(msg))
    return sent


def make_class():
    start = datetime(2026, 10, 20, 18, 0, tzinfo=TZ)  # Tuesday, Israel time (UTC+3 on that date)
    return GymClass("5", "W.O.D, Hall A", start, coach="Coach C", end=start + timedelta(minutes=45))


def test_ics_request_has_utc_times_uid_and_escaped_text(cal):
    ics = ci.build_ics(make_class())
    assert "METHOD:REQUEST" in ics and "STATUS:CONFIRMED" in ics
    assert "DTSTART:20261020T150000Z" in ics and "DTEND:20261020T154500Z" in ics  # 18:00 +03:00 = 15:00Z
    assert "UID:arbox-5@arbox-bot" in ics
    assert "SUMMARY:W.O.D\\, Hall A" in ics
    assert "BEGIN:VALARM" in ics
    assert ics.count("\r\n") == len(ics.splitlines())  # CRLF line endings


def test_ics_cancel_uses_same_uid_and_cancelled_status(cal):
    ics = ci.build_ics(make_class(), "CANCEL")
    assert "METHOD:CANCEL" in ics and "STATUS:CANCELLED" in ics and "SEQUENCE:1" in ics
    assert "UID:arbox-5@arbox-bot" in ics and "VALARM" not in ics


def test_class_without_end_time_defaults_to_one_hour(cal):
    c = replace(make_class(), end=None)
    assert "DTEND:20261020T160000Z" in ci.build_ics(c)


def test_message_is_a_calendar_invite_mail(cal):
    msg = ci.build_message(make_class())
    assert msg["To"] == "me@example.com" and msg["From"] == "bot@example.com"
    assert "Booked" in msg["Subject"]
    cal_part = [p for p in msg.iter_parts() if p.get_content_type() == "text/calendar"][0]
    assert cal_part.get_param("method") == "REQUEST"


async def test_send_uses_smtp_with_login(cal, monkeypatch):
    import smtplib

    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout): log.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): log.append("tls")
        def login(self, u, p): log.append(("login", u, p))
        def send_message(self, m): log.append(("send", m["To"]))

    monkeypatch.undo()  # drop the _send capture; use the real _send with a fake SMTP
    monkeypatch.setattr(ci, "settings", replace(ci.settings, **CFG))
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    assert await ci.send(make_class()) is True
    assert log == [("connect", "smtp.test", 587), "tls", ("login", "bot@example.com", "app-pw"), ("send", "me@example.com")]


async def test_disabled_or_failing_smtp_never_raises(monkeypatch):
    assert await ci.send(make_class()) is False  # not configured
    monkeypatch.setattr(ci, "settings", replace(ci.settings, **CFG))
    monkeypatch.setattr(ci, "_send", lambda m: (_ for _ in ()).throw(OSError("smtp down")))
    assert await ci.send(make_class()) is False


async def test_confirmed_booking_sends_an_invite(client, fake, fast, cal, monkeypatch):  # noqa: F811
    start = future_start()
    fake.add(raw_class(80, start))
    db.add_rule(start.weekday(), f"{start:%H:%M}", "")
    opens_in(monkeypatch, fake, 0.1)
    await run_tick(client)
    assert len(cal) == 1 and "METHOD:REQUEST" in cal[0].get_payload()[1].get_payload()


async def test_dry_run_sends_no_invite(client, fake, fast, cal, monkeypatch):  # noqa: F811
    start = future_start()
    fake.add(raw_class(81, start))
    db.add_rule(start.weekday(), f"{start:%H:%M}", "")
    opens_in(monkeypatch, fake, 0.1)
    monkeypatch.setattr(engine, "settings", replace(engine.settings, dry_run=True, **CFG))
    await run_tick(client)
    assert cal == []


async def test_invite_failure_does_not_break_the_booking(client, fake, fast, cal, monkeypatch):  # noqa: F811
    start = future_start()
    fake.add(raw_class(82, start))
    db.add_rule(start.weekday(), f"{start:%H:%M}", "")
    opens_in(monkeypatch, fake, 0.1)
    monkeypatch.setattr(ci, "_send", lambda m: (_ for _ in ()).throw(OSError("smtp down")))
    await run_tick(client)
    assert fake.booked == [82] and db.get_attempt("82")["status"] == "booked"
    assert any("Calendar invite could not be sent" in n for n in fast)


def test_cancelling_via_api_sends_a_cancellation(cal, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    web = TestClient(main.app)
    target = web.get("/api/schedule", auth=("admin", "pw")).json()[0]
    main.client._booked.add(target["id"])
    r = web.post("/api/cancel", auth=("admin", "pw"), json={"class_id": target["id"], "date": target["start"][:10]})
    assert r.status_code == 200
    assert len(cal) == 1 and "METHOD:CANCEL" in cal[0].get_payload()[1].get_payload()
