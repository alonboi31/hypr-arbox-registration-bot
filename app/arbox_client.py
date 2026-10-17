"""Client for the Arbox / HYPR member API.

The member-side API is private. The endpoint paths, payloads and headers below
are PLACEHOLDERS marked TODO(capture) - fill them in from the mitmproxy
capture (see README). Everything else in the app depends only on the
`ArboxClient` interface, so only this file changes.
"""
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import settings

TZ = ZoneInfo(settings.timezone)


@dataclass
class GymClass:
    id: str
    name: str
    start: datetime  # tz-aware, local
    coach: str = ""
    registered: bool = False
    full: bool = False


class BookingError(Exception):
    pass


class ClassFull(BookingError):
    pass


class NotOpenYet(BookingError):
    pass


class ArboxClient:
    def __init__(self):
        self._http = httpx.AsyncClient(base_url=settings.arbox_base_url, timeout=10)
        self._token: str | None = None

    async def login(self):
        # TODO(capture): real path + body + where the token lives.
        r = await self._http.post(
            "/login",
            json={"email": settings.arbox_email, "password": settings.arbox_password},
        )
        r.raise_for_status()
        self._token = r.json()["token"]  # TODO(capture)

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        if not self._token:
            await self.login()
        headers = {"Authorization": f"Bearer {self._token}"}  # TODO(capture)
        r = await self._http.request(method, path, headers=headers, **kw)
        if r.status_code == 401:  # token expired -> re-login once
            await self.login()
            headers["Authorization"] = f"Bearer {self._token}"
            r = await self._http.request(method, path, headers=headers, **kw)
        return r

    async def get_schedule(self, start: date, end: date) -> list[GymClass]:
        # TODO(capture): real path/params and response mapping.
        r = await self._request(
            "POST", "/schedule", json={"from": start.isoformat(), "to": end.isoformat()}
        )
        r.raise_for_status()
        return [
            GymClass(
                id=str(i["id"]),
                name=i["name"],
                start=datetime.fromisoformat(i["start"]).astimezone(TZ),
                coach=i.get("coach", ""),
                registered=bool(i.get("registered")),
                full=bool(i.get("full")),
            )
            for i in r.json()["classes"]
        ]

    async def book(self, class_id: str):
        """Raise NotOpenYet / ClassFull / BookingError on failure."""
        # TODO(capture): real path/body; map error responses to exceptions.
        r = await self._request("POST", "/book", json={"id": class_id})
        if r.status_code >= 400:
            raise BookingError(f"{r.status_code}: {r.text[:200]}")

    async def join_waitlist(self, class_id: str):
        # TODO(capture): may not exist for every studio.
        r = await self._request("POST", "/waitlist", json={"id": class_id})
        if r.status_code >= 400:
            raise BookingError(f"{r.status_code}: {r.text[:200]}")


class MockClient:
    """Offline stand-in so the GUI/engine can be exercised without Arbox."""

    def __init__(self):
        self._booked: set[str] = set()

    async def get_schedule(self, start: date, end: date) -> list[GymClass]:
        out = []
        d = start
        while d <= end:
            for hh, name in ((7, "WOD"), (18, "WOD"), (19, "Strength")):
                s = datetime(d.year, d.month, d.day, hh, 0, tzinfo=TZ)
                cid = f"mock-{s:%Y%m%d%H}"
                out.append(GymClass(cid, name, s, "Coach", cid in self._booked))
            d += timedelta(days=1)
        return out

    async def book(self, class_id: str):
        if random.random() < 0.3:
            raise NotOpenYet("mock: not open yet")
        self._booked.add(class_id)

    async def join_waitlist(self, class_id: str):
        raise BookingError("mock: no waitlist")


def make_client():
    return MockClient() if settings.mock else ArboxClient()
