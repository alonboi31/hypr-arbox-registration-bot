"""Keep the calendar in step with the registrations: one event per class slot, updated as it changes.

Events are made for the next 14 days from your enabled rules (status "Not open yet" until the
class can be booked), and from any class you are registered for. Each status change sends an
updated invitation; a cancellation (or a class that vanished) sends a cancellation.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import calendar_invite, db
from .calendar_invite import Event
from .config import settings
from .opening import opening_time

log = logging.getLogger("calendar")
HORIZON_DAYS = 14
DEFAULT_LENGTH = timedelta(hours=1)

NOT_OPEN, BOOKED, WAITLIST, FAILED, CANCELLED = "Not open yet", "Booked", "Waitlist", "Failed", "cancelled"
TENTATIVE = {NOT_OPEN, WAITLIST, FAILED}


def slot_uid(start: datetime) -> str:
    return f"arbox-{start:%Y%m%dT%H%M}@arbox-bot"


@dataclass
class Want:
    status: str
    name: str
    start: datetime
    end: datetime
    opens: datetime | None = None


def _event(w: Want) -> Event:
    note = {
        NOT_OPEN: f"Registration opens {w.opens:%a %d/%m %H:%M}. The bot will try to book it then." if w.opens else "Not open for booking yet.",
        BOOKED: "You are booked.",
        WAITLIST: "You are on the waitlist.",
        FAILED: "The bot could not book this class. Book it yourself in the Arbox app.",
    }[w.status]
    return Event(slot_uid(w.start), f"[{w.status}] {w.name}", w.start, w.end, note, w.status in TENTATIVE)


def plan(now, classes, rules, stored, attempt_status, matches, window_days):
    """Pure: what to send. Returns [(Event, method, seq, status)]."""
    by_slot: dict[str, list] = {}
    for c in classes:
        by_slot.setdefault(slot_uid(c.start), []).append(c)

    want: dict[str, Want] = {}
    for c in classes:  # what Arbox says I am registered for, whether or not a rule made it
        if c.start > now and (c.registered or c.in_standby):
            want[slot_uid(c.start)] = Want(BOOKED if c.registered else WAITLIST, c.name, c.start, c.end or c.start + DEFAULT_LENGTH)

    for r in rules:  # what the rules will try to book in the next two weeks
        for i in range(HORIZON_DAYS + 1):
            d = (now + timedelta(days=i)).date()
            if d.weekday() != r["weekday"] or (r.get("once_date") and r["once_date"] != d.isoformat()):
                continue
            hh, mm = map(int, r["time"].split(":"))
            start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=now.tzinfo)
            uid = slot_uid(start)
            if start <= now or start > now + timedelta(days=HORIZON_DAYS) or uid in want:
                continue
            real = next((c for c in by_slot.get(uid, []) if matches(r, c)), None)
            if real is None:  # not published yet: a placeholder from the rule
                name = r["name_contains"] or "Class"
                want[uid] = Want(NOT_OPEN, name, start, start + DEFAULT_LENGTH, opening_time(start))
                continue
            att = attempt_status(real.id)
            end = real.end or real.start + DEFAULT_LENGTH
            if att == "cancelled":
                continue
            if att == "failed":
                want[uid] = Want(FAILED, real.name, real.start, end)
                continue
            opens = opening_time(real.start, real.registration_hours)
            if opens > now:
                want[uid] = Want(NOT_OPEN, real.name, real.start, end, opens)
            else:  # open and being booked right now: keep whatever the calendar already shows
                prev = stored.get(uid)
                keep = prev["status"] if prev and prev["status"] != CANCELLED else NOT_OPEN
                want[uid] = Want(keep, real.name, real.start, end, opens)

    out = []
    for uid, w in want.items():
        s = stored.get(uid)
        if s is None or s["status"] != w.status:
            out.append((_event(w), "REQUEST", s["seq"] + 1 if s else 0, w.status))

    window_end = now + timedelta(days=window_days)
    for uid, s in stored.items():
        if uid in want or s["status"] == CANCELLED:
            continue
        start = datetime.fromisoformat(s["start"])
        if start <= now:
            continue
        if s["status"] in (BOOKED, WAITLIST) and start > window_end:
            continue  # beyond what we can see: do not assume it is gone
        ev = Event(uid, s["title"], start, datetime.fromisoformat(s["end"]), "No longer booked.")
        out.append((ev, "CANCEL", s["seq"] + 1, CANCELLED))
    return out


_failing = False


async def sync(classes, rules, matches, now, notify):
    """Send whatever changed. A mail problem is reported once, never raised."""
    global _failing
    if not calendar_invite.enabled() or settings.dry_run:
        return
    stored = {e["uid"]: e for e in db.list_calendar_events()}
    attempt_status = lambda cid: (db.get_attempt(cid) or {}).get("status")  # noqa: E731
    todo = plan(now, classes, rules, stored, attempt_status, matches, settings.lookahead_days)
    db.prune_calendar_events((now - timedelta(days=2)).isoformat())
    if not todo:
        return
    results = await calendar_invite.send_many([(ev, method, seq) for ev, method, seq, _ in todo])
    for (ev, _method, seq, status), ok in zip(todo, results):
        if ok:
            db.save_calendar_event(ev.uid, ev.title, ev.start.isoformat(), ev.end.isoformat(), status, seq)
    if all(results):
        _failing = False
    elif not _failing:
        _failing = True
        await notify("Calendar invites could not be sent (will retry). Check the SMTP settings with app.mailtest.")
