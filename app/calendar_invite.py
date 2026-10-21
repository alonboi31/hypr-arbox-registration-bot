"""Email calendar invites (.ics) for booked classes, and cancellations for cancelled ones."""
import asyncio
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .arbox_client import GymClass
from .config import settings

log = logging.getLogger("calendar")
DEFAULT_LENGTH = timedelta(hours=1)


def enabled() -> bool:
    return bool(settings.calendar_to and settings.smtp_host and settings.smtp_user and settings.smtp_password)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def uid(c: GymClass) -> str:
    return f"arbox-{c.id}@arbox-bot"


def build_ics(c: GymClass, method: str = "REQUEST") -> str:
    """method REQUEST adds/updates the event, CANCEL removes it (same UID)."""
    if method not in ("REQUEST", "CANCEL"):
        raise ValueError(method)
    sender = settings.smtp_from or settings.smtp_user
    end = c.end or c.start + DEFAULT_LENGTH
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//arbox-bot//EN", "CALSCALE:GREGORIAN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid(c)}",
        f"DTSTAMP:{_utc(datetime.now(timezone.utc))}",
        f"DTSTART:{_utc(c.start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc(c.name)}",
        f"DESCRIPTION:{_esc('Booked by Arbox bot' + (', coach ' + c.coach if c.coach else ''))}",
        f"ORGANIZER:mailto:{sender}",
        f"ATTENDEE;RSVP=FALSE;PARTSTAT=ACCEPTED:mailto:{settings.calendar_to}",
        f"SEQUENCE:{0 if method == 'REQUEST' else 1}",
        f"STATUS:{'CONFIRMED' if method == 'REQUEST' else 'CANCELLED'}",
    ]
    if method == "REQUEST":
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Class soon", "TRIGGER:-PT60M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def build_message(c: GymClass, method: str = "REQUEST") -> EmailMessage:
    when = f"{c.start:%a %d/%m %H:%M}"
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.calendar_to
    msg["Subject"] = ("Booked: " if method == "REQUEST" else "Cancelled: ") + f"{c.name} {when}"
    msg.set_content(f"{c.name}, {when}\n" + ("You are booked." if method == "REQUEST" else "This class was cancelled."))
    msg.add_alternative(build_ics(c, method), subtype="calendar", params={"method": method, "charset": "UTF-8"})
    return msg


def _send(msg: EmailMessage, on_stage=None):
    """Port 465 = SSL from the start; anything else (587) = plain connect then STARTTLS."""
    say = on_stage or (lambda stage: None)
    say("connecting")
    if settings.smtp_port == 465:
        s = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
    else:
        s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with s:
        if settings.smtp_port != 465:
            say("starting TLS")
            s.starttls()
        say("logging in")
        s.login(settings.smtp_user, settings.smtp_password)
        say("sending")
        s.send_message(msg)


async def send(c: GymClass, method: str = "REQUEST") -> bool:
    """Returns True when sent. Never raises: a calendar problem must not break a booking."""
    if not enabled():
        return False
    try:
        await asyncio.to_thread(_send, build_message(c, method))
        return True
    except Exception:
        log.exception("calendar invite failed")
        return False
