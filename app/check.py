"""Safe connection check: logs in and shows what the bot WOULD do. Never books anything.

    docker compose run --rm bot python -m app.check
"""
import asyncio
from datetime import datetime, timedelta

from . import db, engine
from .arbox_client import make_client
from .config import settings
from .opening import opening_time


async def main(client=None) -> int:
    db.init()
    client = client or make_client()
    now = datetime.now(engine.TZ)
    classes = await client.get_schedule(now.date(), (now + timedelta(days=settings.lookahead_days + 3)).date())
    print(f"OK: logged in, {len(classes)} classes in the next {settings.lookahead_days + 3} days")
    if hasattr(client, "membership_id"):
        print(f"OK: booking membership id = {await client.membership_id()}")
    print(f"DRY_RUN={'on (nothing will be booked)' if settings.dry_run else 'OFF (real bookings!)'}   TZ={settings.timezone}")

    rules = [r for r in db.list_rules() if r["enabled"]]
    print(f"\n{len(rules)} active rule(s)")
    hits = [c for c in classes if c.start > now and any(engine.matches(r, c) for r in rules)]
    for c in sorted(hits, key=lambda c: c.start):
        opens = opening_time(c.start, c.registration_hours)
        state = "ALREADY BOOKED" if c.registered else "full -> waitlist" if c.full else "free"
        when = "open now" if opens <= now else f"opens {opens:%a %d/%m %H:%M} (in {str(opens - now).split('.')[0]})"
        print(f"  {c.name:<18} {c.start:%a %d/%m %H:%M}  {when:<42} [{state}]")
    if rules and not hits:
        print("  (no upcoming class matches your rules - check weekday/time/name)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:  # show a readable reason instead of a traceback
        print(f"FAILED: {type(e).__name__}: {e}")
        raise SystemExit(1)
