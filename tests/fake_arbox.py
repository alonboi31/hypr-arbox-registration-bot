"""In-memory fake of the Arbox member API, shaped like the captured traffic.

Plugged into ArboxClient through httpx.MockTransport, so tests exercise the
real client code (headers, payloads, parsing, error mapping) end to end.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Jerusalem")
TOKEN, REFRESH = "tok-1", "ref-1"
MEMBERSHIP_PLAN, MEMBERSHIP_SESSION = 111, 222


def raw_class(cid, start: datetime, *, hours=48, max_users=20, booked=0, category="W.O.D Hall A",
              user_booked=None, user_in_standby=None):
    """A class dict in the same shape as /schedule/betweenDates items."""
    return {
        "id": cid, "status": "active", "max_users": max_users,
        "free": max(max_users - booked, 0), "registered": booked,
        "enable_registration_time": hours,
        "user_booked": user_booked, "user_in_standby": user_in_standby,
        "booked_users": [{"id": i} for i in range(booked)],
        "date_time": {"date": f"{start:%Y-%m-%d %H:%M:%S}", "timezone": "Asia/Jerusalem"},
        "box_categories": {"id": 1, "name": category},
        "coach": {"full_name": "Coach C"},
    }


def _err(status, name):
    return httpx.Response(status, json={
        "statusCode": status, "data": None,
        "error": {"message": "Schedule Exception List", "code": status,
                  "messageToUser": [{"name": name, "value": {}, "message": name}]}})


class FakeArbox:
    def __init__(self):
        self.classes: dict[int, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []   # (METHOD, path, json body)
        self.logins = 0
        self.open_at: datetime | None = None            # None = every class is open
        self.reject_token_once = False                  # simulate an expired token
        self.fail_next = 0                              # next N booking calls return HTTP 500
        self.memberships = [
            {"id": MEMBERSHIP_SESSION, "active": 1, "mt_type": "session"},
            {"id": MEMBERSHIP_PLAN, "active": 1, "mt_type": "plan"},
        ]
        self.booked: list[int] = []
        self.standby: list[int] = []
        self.transport = httpx.MockTransport(self._handle)

    def add(self, c: dict):
        self.classes[c["id"]] = c

    def posts(self, suffix):
        return [b for m, p, b in self.calls if m == "POST" and p.endswith(suffix)]

    # ------------------------------------------------------------------ handler
    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        assert path.startswith("/api/v2"), path
        body = json.loads(req.content) if req.content else {}
        self.calls.append((req.method, path, body))
        short = path[len("/api/v2"):]

        if short == "/user/login":
            self.logins += 1
            # the real server rejected logins lacking the app's anonymous token headers
            if req.headers.get("accesstoken") != "[object Object]" or req.headers.get("refreshtoken") != "[object Object]":
                return httpx.Response(512, text="missing token headers")
            if body != {"email": "me@example.com", "password": "secret"}:
                return httpx.Response(400, json={"error": "bad credentials"})
            return httpx.Response(200, json={"data": {"token": TOKEN, "refreshToken": REFRESH}})

        # every other call needs the app's headers and a valid token
        h = req.headers
        assert h["whitelabel"] == "HYPR-training" and h["version"] == "15" and h["referername"] == "app"
        if self.reject_token_once:
            self.reject_token_once = False
            return httpx.Response(401, json={"error": "expired"})
        if h.get("accesstoken") != TOKEN or h.get("refreshtoken") != REFRESH:
            return httpx.Response(401, json={"error": "unauthorized"})

        if short.startswith("/boxes/59/memberships"):
            return httpx.Response(200, json={"data": self.memberships})

        if short == "/schedule/betweenDates":
            assert body["from"] == body["to"], "app queries one day per request"
            assert body["locations_box_id"] == 48 and body["boxes_id"] == 59
            day = [c for c in self.classes.values() if c["date_time"]["date"].startswith(body["from"])]
            return httpx.Response(200, json={"data": day})

        if short in ("/scheduleUser/insert", "/scheduleStandBy/insert"):
            if self.fail_next:
                self.fail_next -= 1
                return httpx.Response(500, text="boom")
            assert body["membership_user_id"] in (MEMBERSHIP_PLAN, MEMBERSHIP_SESSION)
            assert body["extras"] == {"spot": None, "invited_by": None}
            c = self.classes[body["schedule_id"]]
            if self.open_at is not None and datetime.now(TZ) < self.open_at:
                return _err(425, "registerScheduleDisabled")
            if short.endswith("/scheduleUser/insert"):
                if c["free"] == 0:
                    return _err(425, "scheduleIsFull")  # name is a guess; real one was not captured
                if body["schedule_id"] in self.booked:
                    return _err(425, "alreadyRegistered")
                self.booked.append(body["schedule_id"])
                c["user_booked"] = 900000 + body["schedule_id"]
            else:
                self.standby.append(body["schedule_id"])
                c["user_in_standby"] = 700000 + body["schedule_id"]
            return httpx.Response(200, json={"data": c})

        if short == "/scheduleUser/checkLateCancel":
            return httpx.Response(200, text="OK")

        if short == "/scheduleUser/delete":
            c = self.classes[body["schedule_id"]]
            if c.get("user_booked") != body["schedule_user_id"] or body["late_cancel"] is not False:
                return _err(425, "notRegistered")
            self.booked.remove(body["schedule_id"])
            c["user_booked"] = None
            return httpx.Response(200, json={"data": c})

        return httpx.Response(404, json={"error": f"unhandled {short}"})
