"""Calendar invites: .ics/email content, SMTP, the 14-day status plan, and the engine/API hooks."""
import smtplib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import calendar_invite as ci
from app import calendar_sync as cs
from app import db, engine
from app.arbox_client import GymClass
from app.calendar_invite import Event
from tests.fake_arbox import TZ, raw_class
from tests.test_engine import client, fake, fast, future_start, opens_in, run_tick  # noqa: F401  (fixtures)

CFG = dict(calendar_to="me@example.com", smtp_host="smtp.test", smtp_port=587,
           smtp_user="bot@example.com", smtp_password="app-pw", smtp_from="")
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)  # a Monday


@pytest.fixture
def cfg(monkeypatch):
    for mod in (ci, cs, engine):
        monkeypatch.setattr(mod, "settings", replace(mod.settings, **CFG))


def ev(start=None, title="[Booked] W.O.D, Hall A", tentative=False):
    start = start or datetime(2026, 10, 20, 18, 0, tzinfo=TZ)  # Tuesday, UTC+3
    return Event("arbox-x@arbox-bot", title, start, start + timedelta(minutes=45), "desc", tentative)


# ------------------------------------------------------------------ .ics and email
def test_ics_request_is_an_invitation_for_the_attendee(cfg):
    ics = ci.build_ics(ev(), "REQUEST", 2)
    assert "METHOD:REQUEST" in ics and "STATUS:CONFIRMED" in ics and "SEQUENCE:2" in ics
    assert "DTSTART:20261020T150000Z" in ics and "DTEND:20261020T154500Z" in ics  # 18:00 +03:00 = 15:00Z
    assert "PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:me@example.com" in ics
    assert "ORGANIZER;CN=Arbox bot:mailto:bot@example.com" in ics
    assert "SUMMARY:[Booked] W.O.D\\, Hall A" in ics and "BEGIN:VALARM" in ics
    assert ics.count("\r\n") == len(ics.splitlines())


def test_tentative_events_are_marked_tentative_and_cancel_has_no_alarm(cfg):
    assert "STATUS:TENTATIVE" in ci.build_ics(ev(tentative=True))
    c = ci.build_ics(ev(), "CANCEL", 3)
    assert "METHOD:CANCEL" in c and "STATUS:CANCELLED" in c and "SEQUENCE:3" in c and "VALARM" not in c


def test_message_is_a_real_invitation_mail(cfg):
    msg = ci.build_message(ev(), "REQUEST", 0)
    assert msg["To"] == "me@example.com" and msg["Subject"].startswith("Invitation: [Booked]")
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/calendar" in types and "application/ics" in types
    inline = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert inline.get_param("method") == "REQUEST"
    assert next(p for p in msg.walk() if p.get_content_type() == "application/ics").get_filename() == "invite.ics"
    assert ci.build_message(ev(), "REQUEST", 4)["Subject"].startswith("Updated invitation:")
    assert ci.build_message(ev(), "CANCEL", 5)["Subject"].startswith("Cancelled event:")


# ------------------------------------------------------------------ SMTP
class FakeSMTP:
    log: list = []

    def __init__(self, host, port, timeout): FakeSMTP.log.append(("connect", type(self).__name__, host, port))
    def starttls(self): FakeSMTP.log.append("tls")
    def login(self, u, p): FakeSMTP.log.append(("login", u, p))
    def send_message(self, m): FakeSMTP.log.append(("send", m["Subject"]))
    def close(self): FakeSMTP.log.append("close")


class FakeSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    return FakeSMTP.log


def test_port_587_uses_starttls_and_465_uses_ssl_with_stages(cfg, smtp, monkeypatch):
    stages = []
    ci._send(ci.build_message(ev()), stages.append)
    assert stages == ["connecting", "starting TLS", "logging in", "sending"]
    assert smtp[:3] == [("connect", "FakeSMTP", "smtp.test", 587), "tls", ("login", "bot@example.com", "app-pw")]

    smtp.clear()
    monkeypatch.setattr(ci, "settings", replace(ci.settings, **{**CFG, "smtp_port": 465}))
    stages.clear()
    ci._send(ci.build_message(ev()), stages.append)
    assert smtp[0] == ("connect", "FakeSSL", "smtp.test", 465) and "tls" not in smtp
    assert stages == ["connecting", "logging in", "sending"]


async def test_send_many_uses_one_connection_and_survives_one_bad_message(cfg, smtp, monkeypatch):
    def flaky(self, m):
        if "bad" in m["Subject"]:
            raise smtplib.SMTPException("rejected")
        FakeSMTP.log.append(("send", m["Subject"]))
    monkeypatch.setattr(FakeSMTP, "send_message", flaky)
    items = [(ev(title="[Booked] ok1"), "REQUEST", 0), (ev(title="[Booked] bad"), "REQUEST", 0), (ev(title="[Booked] ok2"), "REQUEST", 0)]
    assert await ci.send_many(items) == [True, False, True]
    assert [e for e in smtp if isinstance(e, tuple) and e[0] == "connect"] == [("connect", "FakeSMTP", "smtp.test", 587)]


async def test_send_many_never_raises_when_login_fails_or_not_configured(cfg, smtp, monkeypatch):
    monkeypatch.setattr(FakeSMTP, "login", lambda self, u, p: (_ for _ in ()).throw(smtplib.SMTPServerDisconnected("closed")))
    assert await ci.send_many([(ev(), "REQUEST", 0)]) == [False]
    monkeypatch.setattr(ci, "settings", replace(ci.settings, calendar_to=""))
    assert await ci.send_many([(ev(), "REQUEST", 0)]) == [False]


# ------------------------------------------------------------------ the plan
def rule(weekday=1, time="18:00", name="", once=None, enabled=1, id=1):
    return dict(id=id, weekday=weekday, time=time, name_contains=name, enabled=enabled, once_date=once)


def klass(cid, start, name="YOGA", **kw):
    return GymClass(str(cid), name, start, registration_hours=48, **kw)


def run_plan(classes=(), rules=(), stored=None, attempts=None, now=NOW):
    attempts = attempts or {}
    return cs.plan(now, list(classes), list(rules), stored or {}, lambda cid: attempts.get(cid), engine.matches, 5)


def stored_event(status, start, seq=0, title="[x] YOGA"):
    return {"uid": cs.slot_uid(start), "title": title, "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(), "status": status, "seq": seq}


TUE1 = datetime(2026, 10, 20, 18, 0, tzinfo=TZ)
TUE2 = datetime(2026, 10, 27, 18, 0, tzinfo=TZ)


def test_rule_makes_not_open_placeholders_two_weeks_ahead():
    out = run_plan(rules=[rule(name="yoga")])
    assert [(o[0].start, o[0].title, o[1], o[2]) for o in out] == [
        (TUE1, "[Not open yet] yoga", "REQUEST", 0), (TUE2, "[Not open yet] yoga", "REQUEST", 0)]
    assert "Registration opens Sun 18/10 18:00" in out[0][0].description  # 48h before
    assert all(o[0].tentative for o in out)


def test_published_class_uses_its_real_name_and_opening_time():
    out = run_plan([klass(1, TUE1, "YOGA (studio)", end=TUE1 + timedelta(minutes=50))], [rule(time="18:00")])
    first = next(o for o in out if o[0].start == TUE1)
    assert first[0].title == "[Not open yet] YOGA (studio)" and first[0].end == TUE1 + timedelta(minutes=50)


def test_registered_waitlisted_and_failed_statuses():
    booked = run_plan([klass(1, TUE1, registered=True)], [rule()])
    assert next(o for o in booked if o[0].start == TUE1)[0].title == "[Booked] YOGA"
    assert not next(o for o in booked if o[0].start == TUE1)[0].tentative
    wait = run_plan([klass(1, TUE1, in_standby=True)], [rule()])
    assert next(o for o in wait if o[0].start == TUE1)[0].title == "[Waitlist] YOGA"
    failed = run_plan([klass(1, TUE1)], [rule()], attempts={"1": "failed"})
    assert next(o for o in failed if o[0].start == TUE1)[0].title == "[Failed] YOGA"


def test_a_class_i_registered_by_hand_gets_an_event_without_any_rule():
    out = run_plan([klass(1, TUE1, registered=True)])
    assert [(o[0].title, o[1]) for o in out] == [("[Booked] YOGA", "REQUEST")]


def test_status_change_sends_an_update_with_higher_sequence_and_no_change_sends_nothing():
    cls = [klass(1, TUE1, registered=True)]
    out = run_plan(cls, [rule()], stored={cs.slot_uid(TUE1): stored_event(cs.NOT_OPEN, TUE1, seq=0)})
    upd = next(o for o in out if o[0].start == TUE1)
    assert upd[1] == "REQUEST" and upd[2] == 1 and upd[3] == cs.BOOKED
    same = run_plan(cls, [], stored={cs.slot_uid(TUE1): stored_event(cs.BOOKED, TUE1, seq=1)})
    assert same == []


def test_cancelled_attempt_cancels_the_event_and_is_not_recreated():
    stored = {cs.slot_uid(TUE1): stored_event(cs.BOOKED, TUE1, seq=1)}
    out = run_plan([klass(1, TUE1)], [rule()], stored=stored, attempts={"1": "cancelled"})
    cancel = [o for o in out if o[1] == "CANCEL"]
    assert len(cancel) == 1 and cancel[0][2] == 2 and cancel[0][3] == cs.CANCELLED
    stored[cs.slot_uid(TUE1)]["status"] = cs.CANCELLED  # after sending, nothing more for that slot
    assert [o for o in run_plan([klass(1, TUE1)], [rule()], stored=stored, attempts={"1": "cancelled"}) if o[0].start == TUE1] == []


def test_disabled_rule_cancels_its_placeholder_but_keeps_a_booking_outside_the_window():
    stored = {cs.slot_uid(TUE2): stored_event(cs.NOT_OPEN, TUE2)}
    assert [o[1] for o in run_plan(rules=[], stored=stored)] == ["CANCEL"]  # placeholder, rule gone
    far = datetime(2026, 11, 3, 18, 0, tzinfo=TZ)  # booked, beyond what the schedule shows
    assert run_plan(rules=[], stored={cs.slot_uid(far): stored_event(cs.BOOKED, far)}) == []
    near_gone = {cs.slot_uid(TUE1): stored_event(cs.BOOKED, TUE1)}  # cancelled in the Arbox app
    assert [o[1] for o in run_plan(rules=[], stored=near_gone)] == ["CANCEL"]


def test_once_rule_only_makes_its_own_date():
    out = run_plan(rules=[rule(once="2026-10-27")])
    assert [o[0].start for o in out] == [TUE2]


def test_open_and_being_booked_keeps_the_current_status_instead_of_flapping():
    soon = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)  # opened 12h ago (48h rule), not booked yet
    stored = {cs.slot_uid(soon): stored_event(cs.NOT_OPEN, soon)}
    assert [o for o in run_plan([klass(1, soon)], [rule(weekday=2, time="08:00")], stored=stored) if o[0].start == soon] == []


# ------------------------------------------------------------------ sync: storage, retry, dry run
async def test_sync_sends_once_stores_state_and_does_not_resend(cfg, monkeypatch):
    sent = []

    async def fake_send(items):
        sent.extend(items)
        return [True] * len(items)

    monkeypatch.setattr(ci, "send_many", fake_send)
    notes = []

    async def notify(m): notes.append(m)

    await cs.sync([], [rule(name="yoga")], engine.matches, NOW, notify)
    assert len(sent) == 2 and {e["status"] for e in db.list_calendar_events()} == {cs.NOT_OPEN}
    sent.clear()
    await cs.sync([], [rule(name="yoga")], engine.matches, NOW, notify)
    assert sent == []  # nothing changed


async def test_failed_send_is_retried_next_time_and_reported_once(cfg, monkeypatch):
    calls = []

    async def failing(items):
        calls.append(len(items))
        return [False] * len(items)

    monkeypatch.setattr(ci, "send_many", failing)
    notes = []

    async def notify(m): notes.append(m)

    await cs.sync([], [rule()], engine.matches, NOW, notify)
    await cs.sync([], [rule()], engine.matches, NOW, notify)
    assert calls == [2, 2] and db.list_calendar_events() == []  # not saved, so it is tried again
    assert len(notes) == 1  # the warning is not repeated every hour


async def test_sync_does_nothing_in_dry_run_or_when_unconfigured(cfg, monkeypatch):
    async def boom(items): raise AssertionError("should not send")
    monkeypatch.setattr(ci, "send_many", boom)

    async def notify(m): pass

    monkeypatch.setattr(cs, "settings", replace(cs.settings, dry_run=True, **CFG))
    await cs.sync([], [rule()], engine.matches, NOW, notify)
    monkeypatch.setattr(cs, "settings", replace(cs.settings, calendar_to=""))
    monkeypatch.setattr(ci, "settings", replace(ci.settings, calendar_to=""))
    await cs.sync([], [rule()], engine.matches, NOW, notify)


# ------------------------------------------------------------------ engine + API end to end
async def test_status_updates_flow_through_a_real_booking(client, fake, fast, cfg, monkeypatch):  # noqa: F811
    sent = []

    async def capture(items):
        sent.extend((e.title, m, s) for e, m, s in items)
        return [True] * len(items)

    monkeypatch.setattr(ci, "send_many", capture)
    start = future_start()
    fake.add(raw_class(90, start, category="YOGA"))
    db.add_rule(start.weekday(), f"{start:%H:%M}", "yoga")
    opens_in(monkeypatch, fake, 0.1)
    monkeypatch.setattr(cs, "opening_time", lambda s, h=None: datetime.now(TZ) + timedelta(hours=1))
    await run_tick(client)
    assert any(t == "[Not open yet] YOGA" and m == "REQUEST" and s == 0 for t, m, s in sent)  # first invite
    sent.clear()
    await run_tick(client)  # the booking has happened, the next tick sees it
    assert ("[Booked] YOGA", "REQUEST", 1) in sent  # same event, updated
    assert db.get_attempt("90")["status"] == "booked"


def test_cancel_through_the_api_leads_to_a_cancellation_mail(cfg, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from app import main

    web = TestClient(main.app)
    target = web.get("/api/schedule", auth=("admin", "pw")).json()[0]
    main.client._booked.add(target["id"])
    r = web.post("/api/cancel", auth=("admin", "pw"), json={"class_id": target["id"], "date": target["start"][:10]})
    assert r.status_code == 200 and db.get_attempt(target["id"])["status"] == "cancelled"
    # the calendar already shows it as booked; the next sync must cancel it
    start = datetime.fromisoformat(target["start"])
    db.save_calendar_event(cs.slot_uid(start), "[Booked] X", start.isoformat(), (start + timedelta(hours=1)).isoformat(), cs.BOOKED, 1)
    sent = []

    async def capture(items):
        sent.extend((e.uid, m, s) for e, m, s in items)
        return [True] * len(items)

    monkeypatch.setattr(ci, "send_many", capture)

    async def go():
        classes = await main.client.get_schedule(start.date(), start.date())
        async def notify(m): pass
        await cs.sync(classes, [], engine.matches, start - timedelta(days=1), notify)

    asyncio.run(go())
    assert (cs.slot_uid(start), "CANCEL", 2) in sent


def test_mailtest_command_reports_each_outcome(cfg, smtp, monkeypatch, capsys):
    from app import mailtest
    monkeypatch.setattr(mailtest, "settings", replace(mailtest.settings, **CFG))
    assert mailtest.main() == 0 and "test invite sent" in capsys.readouterr().out
    assert any(isinstance(e, tuple) and e[0] == "send" and "Invitation" in e[1] for e in smtp)

    monkeypatch.setattr(FakeSMTP, "login", lambda self, u, p: (_ for _ in ()).throw(smtplib.SMTPServerDisconnected("closed")))
    assert mailtest.main() == 1 and "FAILED while logging in: SMTPServerDisconnected: closed" in capsys.readouterr().out

    monkeypatch.setattr(mailtest, "settings", replace(mailtest.settings, **{**CFG, "smtp_password": ""}))
    assert mailtest.main() == 1 and "SMTP_PASSWORD" in capsys.readouterr().out
