import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import db
from .arbox_client import BookingError, ClassFull, GymClass
from .config import settings
from .opening import opening_time

log = logging.getLogger("engine")
TZ = ZoneInfo(settings.timezone)

TICK_SECONDS = 30
WAKE_AHEAD = timedelta(seconds=90)   # start waiting this long before opening
RETRY_WINDOW = timedelta(seconds=120)  # keep retrying this long after opening
RETRY_EVERY = 0.4


def matches(rule: dict, c: GymClass) -> bool:
    if not rule["enabled"] or rule["weekday"] != c.start.weekday():
        return False
    if rule["time"] != f"{c.start:%H:%M}":
        return False
    needle = rule["name_contains"].lower()
    return not needle or needle in c.name.lower()


def label(c: GymClass) -> str:
    return f"{c.name} {c.start:%a %d/%m %H:%M}"


async def notify(msg: str):
    log.info(msg)
    if settings.ntfy_topic:
        try:
            async with httpx.AsyncClient(timeout=5) as h:
                await h.post(f"https://ntfy.sh/{settings.ntfy_topic}", content=msg.encode())
        except Exception:
            log.exception("notify failed")


async def book_when_open(client, c: GymClass):
    opens = opening_time(c.start)
    delay = (opens - datetime.now(TZ)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    deadline = datetime.now(TZ) + RETRY_WINDOW
    last = ""
    while datetime.now(TZ) < deadline:
        try:
            await client.book(c.id)
            db.set_attempt(c.id, "booked")
            await notify(f"Booked: {label(c)}")
            return
        except ClassFull:
            try:
                await client.join_waitlist(c.id)
                db.set_attempt(c.id, "waitlisted")
                await notify(f"Class full, joined waitlist: {label(c)}")
            except BookingError as e:
                db.set_attempt(c.id, "failed", f"full, waitlist failed: {e}")
                await notify(f"Class full, no waitlist: {label(c)}")
            return
        except Exception as e:  # NotOpenYet, network blips, etc. -> retry
            last = str(e)
            await asyncio.sleep(RETRY_EVERY)
    db.set_attempt(c.id, "failed", last)
    await notify(f"FAILED to book {label(c)}: {last}")


async def tick(client, tasks: set):
    now = datetime.now(TZ)
    rules = [r for r in db.list_rules() if r["enabled"]]
    if not rules:
        return
    classes = await client.get_schedule(
        now.date(), (now + timedelta(days=settings.lookahead_days)).date()
    )
    for c in classes:
        if c.registered or c.start < now:
            continue
        if not any(matches(r, c) for r in rules):
            continue
        if opening_time(c.start) - now > WAKE_AHEAD:
            continue
        if not db.claim_attempt(c.id, label(c)):
            continue
        t = asyncio.create_task(book_when_open(client, c))
        tasks.add(t)
        t.add_done_callback(tasks.discard)


async def run_forever(client):
    tasks: set = set()
    while True:
        try:
            await tick(client, tasks)
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(TICK_SECONDS)
