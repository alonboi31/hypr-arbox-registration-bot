import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import db, engine
from .arbox_client import make_client
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


class EnabledIn(BaseModel):
    enabled: bool


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/rules")
def rules():
    return db.list_rules()


@app.post("/api/rules")
def add_rule(r: RuleIn):
    db.add_rule(r.weekday, r.time, r.name_contains)
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
            "matched": any(engine.matches(r, c) for r in active),
        }
        for c in classes
        if c.start > now
    ]


@app.get("/api/attempts")
def attempts():
    return db.recent_attempts()
