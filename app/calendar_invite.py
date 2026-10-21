"""Email calendar invitations (.ics, iTIP) and the SMTP plumbing to send them."""
import asyncio
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("calendar")


@dataclass
class Event:
    uid: str
    title: str
    start: datetime          # tz-aware
    end: datetime            # tz-aware
    description: str = ""
    tentative: bool = False  # shown as "tentative" in calendars (not booked yet / waitlist)


def enabled() -> bool:
    return bool(settings.calendar_to and settings.smtp_host and settings.smtp_user and settings.smtp_password)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(ev: Event, method: str = "REQUEST", seq: int = 0) -> str:
    """REQUEST creates or updates the event with this UID (higher `seq` wins); CANCEL removes it."""
    if method not in ("REQUEST", "CANCEL"):
        raise ValueError(method)
    sender = settings.smtp_from or settings.smtp_user
    status = "CANCELLED" if method == "CANCEL" else "TENTATIVE" if ev.tentative else "CONFIRMED"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//arbox-bot//EN", "CALSCALE:GREGORIAN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{ev.uid}",
        f"DTSTAMP:{_utc(datetime.now(timezone.utc))}",
        f"DTSTART:{_utc(ev.start)}",
        f"DTEND:{_utc(ev.end)}",
        f"SUMMARY:{_esc(ev.title)}",
        f"DESCRIPTION:{_esc(ev.description)}",
        f"ORGANIZER;CN=Arbox bot:mailto:{sender}",
        f"ATTENDEE;CN=Me;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{settings.calendar_to}",
        f"SEQUENCE:{seq}",
        f"STATUS:{status}",
        "TRANSP:OPAQUE",
    ]
    if method == "REQUEST":
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Class soon", "TRIGGER:-PT60M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def build_message(ev: Event, method: str = "REQUEST", seq: int = 0) -> EmailMessage:
    """A real invitation email: calendar part inline (so Gmail shows the invite card) plus invite.ics."""
    when = f"{ev.start:%a %d %b %H:%M}"
    if method == "CANCEL":
        subject = f"Cancelled event: {ev.title} @ {when}"
    else:
        subject = f"{'Invitation' if seq == 0 else 'Updated invitation'}: {ev.title} @ {when}"
    msg = EmailMessage()
    msg["From"] = f"Arbox bot <{settings.smtp_from or settings.smtp_user}>"
    msg["To"] = settings.calendar_to
    msg["Subject"] = subject
    msg.set_content(f"{ev.title}\n{when}\n\n{ev.description}\n")
    ics = build_ics(ev, method, seq)
    msg.add_alternative(ics, subtype="calendar", params={"method": method, "charset": "UTF-8"})
    msg.add_attachment(ics.encode("utf-8"), maintype="application", subtype="ics", filename="invite.ics")
    return msg


def _connect(on_stage=None) -> smtplib.SMTP:
    """Open a logged-in SMTP connection. Port 465 = SSL from the start; otherwise STARTTLS."""
    say = on_stage or (lambda stage: None)
    say("connecting")
    ssl = settings.smtp_port == 465
    s = (smtplib.SMTP_SSL if ssl else smtplib.SMTP)(settings.smtp_host, settings.smtp_port, timeout=20)
    try:
        if not ssl:
            say("starting TLS")
            s.starttls()
        say("logging in")
        s.login(settings.smtp_user, settings.smtp_password)
    except Exception:
        s.close()
        raise
    return s


def _send(msg: EmailMessage, on_stage=None):
    s = _connect(on_stage)
    try:
        (on_stage or (lambda stage: None))("sending")
        s.send_message(msg)
    finally:
        s.close()


def _send_many(msgs: list[EmailMessage]) -> list[bool]:
    """One connection for the whole batch. A message that fails does not stop the others."""
    try:
        s = _connect()
    except Exception:
        log.exception("calendar mail login failed")
        return [False] * len(msgs)
    results = []
    try:
        for m in msgs:
            try:
                s.send_message(m)
                results.append(True)
            except Exception:
                log.exception("calendar mail send failed")
                results.append(False)
    finally:
        s.close()
    return results


async def send_many(items: list[tuple[Event, str, int]]) -> list[bool]:
    """items = (event, method, seq). Never raises."""
    if not items or not enabled():
        return [False] * len(items)
    try:
        return await asyncio.to_thread(_send_many, [build_message(*i) for i in items])
    except Exception:
        log.exception("calendar invite failed")
        return [False] * len(items)
