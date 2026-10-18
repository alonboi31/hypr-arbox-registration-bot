"""Client for the Arbox / HYPR member API (apiappv2.arboxapp.com).

Endpoints and payloads come from a traffic capture of the HYPR iOS app.
Everything else in the app depends only on the client interface, so this is
the only file that talks to Arbox.
"""
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import settings

log = logging.getLogger("arbox")
TZ = ZoneInfo(settings.timezone)

# 425 "Schedule Exception List" reason that just means "registration not open yet".
NOT_OPEN_REASONS = {"registerScheduleDisabled"}


@dataclass
class GymClass:
    id: str
    name: str
    start: datetime  # tz-aware, local
    coach: str = ""
    registered: bool = False   # I'm booked
    in_standby: bool = False   # I'm on the waitlist
    full: bool = False
    registration_hours: int | None = None  # hours before start that booking opens
    schedule_user_id: int | None = None  # my booking's id (needed to cancel it)


class LoginError(Exception):
    pass


# Before login the app sends these two headers with this literal value (a JS quirk in
# the app: it stringifies an empty object). Arbox accepted it in the capture, so mirror it.
ANON_TOKENS = {"accesstoken": "[object Object]", "refreshtoken": "[object Object]"}


class BookingError(Exception):
    """Booking failed for a reason retrying will not fix."""


class ClassFull(BookingError):
    pass


class NotOpenYet(Exception):
    """Registration has not opened - retry."""


def _parse_class(c: dict) -> GymClass:
    dt = c["date_time"]
    start = datetime.fromisoformat(dt["date"]).replace(tzinfo=ZoneInfo(dt.get("timezone") or settings.timezone))
    cat = c.get("box_categories") or {}
    coach = c.get("coach") or {}
    return GymClass(
        id=str(c["id"]),
        name=cat.get("name", ""),
        start=start.astimezone(TZ),
        coach=(coach.get("full_name") or coach.get("first_name") or "") if isinstance(coach, dict) else "",
        registered=bool(c.get("user_booked")),
        in_standby=bool(c.get("user_in_standby")),
        full=c.get("free", 1) == 0 or len(c.get("booked_users") or []) >= c.get("max_users", 10**9),
        registration_hours=c.get("enable_registration_time"),
        schedule_user_id=c.get("user_booked") or None,
    )


class ArboxClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            transport=transport,
            base_url=settings.arbox_base_url,
            timeout=10,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
                "whitelabel": settings.whitelabel,
                "version": "15",
                "referername": "app",
                "user-agent": "HYPRtraining/7200004",
            },
        )
        self._tokens: dict[str, str] | None = None
        self._membership_id: int | None = settings.membership_user_id

    async def login(self):
        r = await self._http.post(
            "/user/login",
            json={"email": settings.arbox_email, "password": settings.arbox_password},
            headers=ANON_TOKENS,
        )
        if r.status_code != 200:
            raise LoginError(f"login failed: HTTP {r.status_code}: {r.text[:300]!r}")
        d = r.json()["data"]
        self._tokens = {"accesstoken": d["token"], "refreshtoken": d["refreshToken"]}

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        if not self._tokens:
            await self.login()
        r = await self._http.request(method, path, headers=self._tokens, **kw)
        if r.status_code in (401, 403):  # token rejected -> log in again, once
            await self.login()
            r = await self._http.request(method, path, headers=self._tokens, **kw)
        return r

    async def membership_id(self) -> int:
        """The membership_user_id sent with every booking (auto-picks an active plan)."""
        if self._membership_id:
            return self._membership_id
        r = await self._request("GET", f"/boxes/{settings.box_id}/memberships/1/false")
        r.raise_for_status()
        active = [m for m in r.json()["data"] if m.get("active")]
        if not active:
            raise BookingError("no active membership found")
        plans = [m for m in active if m.get("mt_type") == "plan"]
        self._membership_id = (plans or active)[0]["id"]
        return self._membership_id

    async def get_schedule(self, start: date, end: date) -> list[GymClass]:
        out: list[GymClass] = []
        d = start
        while d <= end:  # the app queries one day per request
            r = await self._request(
                "POST",
                "/schedule/betweenDates",
                json={
                    "from": d.isoformat(),
                    "to": d.isoformat(),
                    "locations_box_id": settings.location_id,
                    "boxes_id": settings.box_id,
                },
            )
            r.raise_for_status()
            out += [_parse_class(c) for c in r.json()["data"] if c.get("status") == "active"]
            d += timedelta(days=1)
        return out

    async def get_class(self, class_id: str, day: date) -> GymClass | None:
        return next((c for c in await self.get_schedule(day, day) if c.id == class_id), None)

    @staticmethod
    def _reasons(r: httpx.Response) -> list[str]:
        try:
            return [m["name"] for m in r.json()["error"]["messageToUser"]]
        except Exception:
            return []

    async def _insert(self, path: str, class_id: str):
        r = await self._request(
            "POST",
            path,
            json={
                "schedule_id": int(class_id),
                "membership_user_id": await self.membership_id(),
                "extras": {"spot": None, "invited_by": None},
            },
        )
        if r.status_code == 200:
            return
        reasons = self._reasons(r)
        if r.status_code == 425 and reasons and set(reasons) <= NOT_OPEN_REASONS:
            raise NotOpenYet(",".join(reasons))
        if r.status_code >= 500 or r.status_code == 429:
            raise NotOpenYet(f"server {r.status_code}")  # transient -> retry
        raise BookingError(f"{r.status_code} {','.join(reasons) or r.text[:200]}")

    async def book(self, class_id: str):
        """NotOpenYet = retry; BookingError = give up (or check for full/waitlist)."""
        if settings.dry_run:
            log.info("DRY_RUN: would book %s", class_id)
            return
        await self._insert("/scheduleUser/insert", class_id)

    async def cancel(self, c: GymClass):
        """Cancel my booking (never a late cancel: Arbox may charge for those)."""
        if not c.schedule_user_id:
            raise BookingError("not registered for this class, nothing to cancel")
        r = await self._request("POST", "/scheduleUser/checkLateCancel", json={"schedule_id": int(c.id)})
        if r.status_code != 200:
            raise BookingError(f"cancel check failed: {r.status_code} {r.text[:200]}")
        r = await self._request(
            "POST",
            "/scheduleUser/delete",
            json={"schedule_user_id": c.schedule_user_id, "schedule_id": int(c.id), "late_cancel": False},
        )
        if r.status_code != 200:
            raise BookingError(f"cancel failed: {r.status_code} {','.join(self._reasons(r)) or r.text[:200]}")

    async def join_waitlist(self, class_id: str):
        if settings.dry_run:
            log.info("DRY_RUN: would join waitlist %s", class_id)
            return
        await self._insert("/scheduleStandBy/insert", class_id)


class MockClient:
    """Offline stand-in so the GUI/engine can be exercised without Arbox."""

    def __init__(self):
        self._booked: set[str] = set()

    async def get_schedule(self, start: date, end: date) -> list[GymClass]:
        out = []
        d = start
        while d <= end:
            for hh, name in ((7, "W.O.D Hall A"), (18, "W.O.D Hall A"), (19, "HYROX")):
                s = datetime(d.year, d.month, d.day, hh, 0, tzinfo=TZ)
                cid = f"mock-{s:%Y%m%d%H}"
                out.append(GymClass(cid, name, s, "Coach", cid in self._booked, registration_hours=48))
            d += timedelta(days=1)
        return out

    async def get_class(self, class_id: str, day: date):
        return next((c for c in await self.get_schedule(day, day) if c.id == class_id), None)

    async def book(self, class_id: str):
        if random.random() < 0.3:
            raise NotOpenYet("mock: not open yet")
        self._booked.add(class_id)

    async def join_waitlist(self, class_id: str):
        raise BookingError("mock: no waitlist")


def make_client():
    return MockClient() if settings.mock else ArboxClient()
