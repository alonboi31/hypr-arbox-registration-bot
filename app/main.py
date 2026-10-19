import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import db, engine
from .arbox_client import BookingError, make_client
from .config import settings
from .opening import opening_time

logging.basicConfig(level=logging.INFO)
client = make_client()
security = HTTPBasic()
STATIC = Path(__file__).parent / "static"


def auth(cred: HTTPBasicCredentials = Depends(security)):
    ok = settings.ui_password and secrets.compare_digest(
        cred.password.encode(), settings.ui_password.encode()
    ) and secrets.compare_digest(cred.username.encode(), settings.ui_user.encode())
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(engine.run_forever(client))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth)])


class RuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    name_contains: str = ""
    once: bool = False  # only the next matching class, then the rule switches itself off
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # pin the one-time rule to this day


class CancelIn(BaseModel):
    class_id: str
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class EnabledIn(BaseModel):
    enabled: bool


def next_occurrence(weekday: int, time: str) -> date:
    """The next calendar day (today if the time has not passed yet) that falls on `weekday`."""
    now = datetime.now(engine.TZ)
    hh, mm = map(int, time.split(":"))
    d = now.date()
    for _ in range(8):
        if d.weekday() == weekday and datetime(d.year, d.month, d.day, hh, mm, tzinfo=engine.TZ) > now:
            return d
        d += timedelta(days=1)
    return d


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/rules")
def rules():
    return db.list_rules()


@app.post("/api/rules")
def add_rule(r: RuleIn):
    once_date = None
    if r.once or r.date:
        once_date = r.date or next_occurrence(r.weekday, r.time).isoformat()
    db.add_rule(r.weekday, r.time, r.name_contains, once_date)
    engine.poke()
    return {"ok": True}


@app.patch("/api/rules/{rule_id}")
def toggle_rule(rule_id: int, body: EnabledIn):
    db.set_rule_enabled(rule_id, body.enabled)
    engine.poke()
    return {"ok": True}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    db.delete_rule(rule_id)
    return {"ok": True}


@app.get("/api/schedule")
async def schedule():
    """Upcoming classes, so the UI can offer one-click 'always book this'."""
    now = datetime.now(engine.TZ)
    classes = await client.get_schedule(now.date(), (now + timedelta(days=7)).date())
    active = [r for r in db.list_rules() if r["enabled"]]
    cancelled = {a["class_id"] for a in db.recent_attempts(500) if a["status"] == "cancelled"}
    return [
        {
            "id": c.id,
            "name": c.name,
            "coach": c.coach,
            "start": c.start.isoformat(),
            "weekday": c.start.weekday(),
            "time": f"{c.start:%H:%M}",
            "opens": opening_time(c.start, c.registration_hours).isoformat(),
            "registered": c.registered or c.in_standby,
            "matched": c.id not in cancelled and any(engine.matches(r, c) for r in active),
        }
        for c in classes
        if c.start > now
    ]


@app.post("/api/cancel")
async def cancel(body: CancelIn):
    """Cancel my booking (or leave the waitlist) for one class."""
    c = await client.get_class(body.class_id, date.fromisoformat(body.date))
    if not c or not (c.registered or c.in_standby):
        raise HTTPException(404, "You are not registered for that class.")
    try:
        await client.cancel(c)
    except BookingError as e:
        raise HTTPException(409, str(e))
    # remember it, so a matching rule does not book the class again straight away
    db.claim_attempt(c.id, engine.label(c))
    db.set_attempt(c.id, "cancelled", "cancelled by you")
    if c.registered:  # not for waitlist spots, which never got an invite
        await engine.invite(c, "CANCEL")
    return {"ok": True}


@app.get("/api/attempts")
def attempts():
    return db.recent_attempts()
