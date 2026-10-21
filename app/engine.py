import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import calendar_invite, calendar_sync, db
from .arbox_client import BookingError, GymClass, NotOpenYet
from .config import settings
from .opening import opening_time

log = logging.getLogger("engine")
TZ = ZoneInfo(settings.timezone)

TICK_SECONDS = 3600  # one schedule request per hour
WAKE_AHEAD = timedelta(seconds=90)   # margin before opening (on top of one tick)
PREFLIGHT = timedelta(seconds=90)    # refresh the class + warm the connection this long before opening
LEAD = timedelta(seconds=0.15)       # first attempt this much before opening (~half a round trip)
RETRY_WINDOW = timedelta(seconds=120)  # keep retrying this long after opening
RETRY_EVERY = 0.25                   # fast phase: first FAST_PHASE seconds after the first attempt
FAST_PHASE = 3.0


def retry_delay(elapsed: float) -> float:
    """Fast at the contested moment, then ease off so we never hammer Arbox."""
    if elapsed < FAST_PHASE:
        return RETRY_EVERY
    return 1.0 if elapsed < 60 else 2.0


def matches(rule: dict, c: GymClass) -> bool:
    if not rule["enabled"] or rule["weekday"] != c.start.weekday():
        return False
    if rule["time"] != f"{c.start:%H:%M}":
        return False
    if rule.get("once_date") and rule["once_date"] != f"{c.start:%Y-%m-%d}":
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


VERIFY_TRIES = 3
VERIFY_EVERY = 1.0


async def verify(client, c: GymClass, waitlisted: bool = False) -> bool:
    """Re-read the class from Arbox and confirm we are really in (or on its waitlist)."""
    for i in range(VERIFY_TRIES):
        if i:
            await asyncio.sleep(VERIFY_EVERY)
        try:
            fresh = await client.get_class(c.id, c.start.date())
        except Exception:
            log.exception("verify failed")
            continue
        if fresh and (fresh.in_standby if waitlisted else fresh.registered):
            return True
    return False


async def waitlist(client, c: GymClass):
    try:
        await client.join_waitlist(c.id)
        if not settings.dry_run and not await verify(client, c, waitlisted=True):
            db.set_attempt(c.id, "failed", "waitlist accepted but not confirmed on Arbox")
            await notify(f"UNCONFIRMED waitlist, check the app: {label(c)}")
            return
        db.set_attempt(c.id, "waitlisted")
        await notify(f"Class full, joined waitlist: {label(c)}")
    except BookingError as e:
        db.set_attempt(c.id, "failed", f"full, waitlist failed: {e}")
        await notify(f"Class full, waitlist failed: {label(c)} ({e})")


async def _sleep_until(t: datetime):
    delay = (t - datetime.now(TZ)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


async def _book_when_open(client, c: GymClass):
    opens = opening_time(c.start, c.registration_hours)
    await _sleep_until(opens - PREFLIGHT)

    # Pre-flight: one fresh look at the class (also logs in and opens the HTTP connection),
    # plus the membership id, so nothing but the booking POST is left for the critical moment.
    fresh = await client.get_class(c.id, c.start.date())
    if fresh is None:
        db.set_attempt(c.id, "failed", "class disappeared from schedule")
        await notify(f"FAILED to book {label(c)}: class disappeared from schedule")
        return
    if fresh.registered or fresh.in_standby:
        db.set_attempt(c.id, "booked" if fresh.registered else "waitlisted")
        return
    c = fresh
    opens = opening_time(c.start, c.registration_hours)  # trust Arbox's latest value
    if not settings.dry_run:
        await client.membership_id()

    await _sleep_until(opens - LEAD)
    started = datetime.now(TZ)
    deadline = started + RETRY_WINDOW
    last = ""
    while datetime.now(TZ) < deadline:
        try:
            await client.book(c.id)
            if settings.dry_run:
                db.set_attempt(c.id, "dry-run", "would have booked")
                await notify(f"DRY RUN - would book: {label(c)}")
            elif await verify(client, c):
                db.set_attempt(c.id, "booked")
                await notify(f"Booked (confirmed on Arbox): {label(c)}")
            else:
                db.set_attempt(c.id, "failed", "booking accepted but class not registered on Arbox")
                await notify(f"UNCONFIRMED booking, check the app: {label(c)}")
            return
        except BookingError as e:  # a real refusal: full class? membership limit?
            fresh = await client.get_class(c.id, c.start.date())
            if fresh and fresh.registered:
                db.set_attempt(c.id, "booked")
                return
            if fresh and fresh.full:
                await waitlist(client, c)
            else:
                db.set_attempt(c.id, "failed", str(e))
                await notify(f"FAILED to book {label(c)}: {e}")
            return
        except Exception as e:  # NotOpenYet, network blips -> retry, backing off
            last = str(e)
            await asyncio.sleep(retry_delay((datetime.now(TZ) - started).total_seconds()))
    db.set_attempt(c.id, "failed", last)
    await notify(f"FAILED to book {label(c)}: {last}")


async def book_when_open(client, c: GymClass):
    """Never let a booking task die silently: record and report any crash."""
    try:
        await _book_when_open(client, c)
        finish_once(c)
        poke()
    except Exception as e:
        log.exception("booking task crashed")
        db.set_attempt(c.id, "failed", f"{type(e).__name__}: {e}")
        await notify(f"FAILED to book {label(c)}: {type(e).__name__}: {e}")
        finish_once(c)
        poke()


def finish_once(c: GymClass):
    """One-time rules are used up after their class was attempted (but not by a rehearsal)."""
    if settings.dry_run:
        return
    done = [r["id"] for r in db.list_rules() if r.get("once_date") and r["enabled"] and matches(r, c)]
    if done:
        db.finish_once_rules(done)


async def tick(client, tasks: set):
    now = datetime.now(TZ)
    if not settings.dry_run:
        db.release_dry_runs()
    db.expire_once_rules(f"{now:%Y-%m-%d}")
    rules = [r for r in db.list_rules() if r["enabled"]]
    if not rules and not calendar_invite.enabled():
        return
    classes = await client.get_schedule(
        now.date(), (now + timedelta(days=settings.lookahead_days)).date()
    )
    for c in classes:
        if c.registered or c.in_standby or c.start < now:
            continue
        if not any(matches(r, c) for r in rules):
            continue
        # claim anything opening before the next tick would see it; the task sleeps until then
        if opening_time(c.start, c.registration_hours) - now > timedelta(seconds=TICK_SECONDS) + WAKE_AHEAD:
            continue
        if not db.claim_attempt(c.id, label(c)):
            continue
        t = asyncio.create_task(book_when_open(client, c))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    try:
        await calendar_sync.sync(classes, rules, matches, now, notify)
    except Exception:
        log.exception("calendar sync failed")


_wake: asyncio.Event | None = None


def poke():
    """Rules changed: run a schedule check now instead of waiting for the hourly one."""
    if _wake:
        _wake.set()


async def run_forever(client):
    global _wake
    _wake = asyncio.Event()
    tasks: set = set()
    while True:
        try:
            await tick(client, tasks)
        except Exception:
            log.exception("tick failed")
        try:
            await asyncio.wait_for(_wake.wait(), TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
