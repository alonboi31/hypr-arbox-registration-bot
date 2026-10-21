"""Send one test calendar invite so you can check the email settings without waiting for a booking.

    docker compose run --rm bot python -m app.mailtest
"""
from datetime import datetime, timedelta

from . import calendar_invite
from .arbox_client import GymClass
from .config import settings
from .engine import TZ


def main() -> int:
    missing = [n for n, v in [("CALENDAR_TO", settings.calendar_to), ("SMTP_HOST", settings.smtp_host),
                              ("SMTP_USER", settings.smtp_user), ("SMTP_PASSWORD", settings.smtp_password)] if not v]
    if missing:
        print("NOT CONFIGURED: these .env lines are empty:", ", ".join(missing))
        return 1
    start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    c = GymClass("test", "Arbox bot test class", start, coach="Test", end=start + timedelta(hours=1))
    print(f"Sending from {settings.smtp_user} via {settings.smtp_host}:{settings.smtp_port} to {settings.calendar_to} ...")
    try:
        calendar_invite._send(calendar_invite.build_message(c))
    except Exception as e:  # show the real reason, not a traceback
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1
    print("OK: test invite sent. Check the inbox (and spam) of", settings.calendar_to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
