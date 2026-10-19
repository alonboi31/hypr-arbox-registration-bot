"""Web API / GUI tests against the real FastAPI app (mock Arbox client, real SQLite)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.fake_arbox import TZ

AUTH = ("admin", "pw")


@pytest.fixture
def web():
    # no `with`: don't start the background engine loop
    return TestClient(app)


def test_everything_requires_auth(web):
    for method, path in [("get", "/"), ("get", "/api/rules"), ("get", "/api/schedule"),
                         ("get", "/api/attempts"), ("post", "/api/rules"), ("delete", "/api/rules/1")]:
        assert getattr(web, method)(path).status_code == 401, path
    assert web.get("/api/rules", auth=("admin", "wrong")).status_code == 401
    assert web.get("/api/rules", auth=("root", "pw")).status_code == 401


def test_gui_page_is_served(web):
    r = web.get("/", auth=AUTH)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    for needle in ("Auto-booking rules", "Registered", "Schedule", "Recent attempts", "/api/rules", "/api/schedule", "esc("):
        assert needle in r.text


def test_rule_lifecycle_through_api(web):
    assert web.get("/api/rules", auth=AUTH).json() == []
    r = web.post("/api/rules", auth=AUTH, json={"weekday": 2, "time": "18:00", "name_contains": "hall a"})
    assert r.status_code == 200
    rule = web.get("/api/rules", auth=AUTH).json()[0]
    assert (rule["weekday"], rule["time"], rule["name_contains"], rule["enabled"]) == (2, "18:00", "hall a", 1)

    assert web.patch(f"/api/rules/{rule['id']}", auth=AUTH, json={"enabled": False}).status_code == 200
    assert web.get("/api/rules", auth=AUTH).json()[0]["enabled"] == 0
    assert web.delete(f"/api/rules/{rule['id']}", auth=AUTH).status_code == 200
    assert web.get("/api/rules", auth=AUTH).json() == []


@pytest.mark.parametrize("bad", [
    {"weekday": 7, "time": "18:00"},
    {"weekday": -1, "time": "18:00"},
    {"weekday": 1, "time": "6pm"},
    {"weekday": 1, "time": "7:00"},
    {"time": "18:00"},
])
def test_rule_validation(web, bad):
    assert web.post("/api/rules", auth=AUTH, json=bad).status_code == 422
    assert db.list_rules() == []


def test_schedule_lists_future_classes_with_opening_and_match_flags(web):
    day = datetime.now(TZ) + timedelta(days=2)
    db.add_rule(day.weekday(), "18:00", "hall a")
    items = web.get("/api/schedule", auth=AUTH).json()
    assert items and all(datetime.fromisoformat(c["start"]) > datetime.now(TZ) for c in items)
    mine = [c for c in items if c["weekday"] == day.weekday() and c["time"] == "18:00"]
    assert mine and all(c["matched"] for c in mine)
    assert not any(c["matched"] for c in items if c["time"] != "18:00")
    c = items[0]
    assert datetime.fromisoformat(c["start"]) - datetime.fromisoformat(c["opens"]) == timedelta(hours=48)
    assert {"id", "name", "coach", "registered"} <= set(c)


def test_disabled_rule_is_not_marked_matched(web):
    day = datetime.now(TZ) + timedelta(days=2)
    db.add_rule(day.weekday(), "18:00", "")
    db.set_rule_enabled(db.list_rules()[0]["id"], False)
    assert not any(c["matched"] for c in web.get("/api/schedule", auth=AUTH).json())


def test_attempts_endpoint_shows_history(web):
    db.claim_attempt("a1", "WOD Wed 18:00")
    db.set_attempt("a1", "booked")
    rows = web.get("/api/attempts", auth=AUTH).json()
    assert rows[0]["class_label"] == "WOD Wed 18:00" and rows[0]["status"] == "booked"


def test_refuses_to_serve_when_no_ui_password_is_set(web, monkeypatch):
    from dataclasses import replace
    from app import main
    monkeypatch.setattr(main, "settings", replace(main.settings, ui_password=""))
    assert web.get("/api/rules", auth=("admin", "")).status_code == 401


def test_app_starts_and_stops_with_background_engine():
    with TestClient(app) as c:  # runs lifespan: db.init + engine loop task
        assert c.get("/api/rules", auth=AUTH).status_code == 200
    # leaving the block cancels the loop without errors


def test_once_rule_via_api_gets_next_date_or_the_given_one(web):
    web.post("/api/rules", auth=AUTH, json={"weekday": 2, "time": "18:00", "once": True})
    web.post("/api/rules", auth=AUTH, json={"weekday": 2, "time": "18:00", "once": True, "date": "2026-12-09"})
    web.post("/api/rules", auth=AUTH, json={"weekday": 2, "time": "18:00"})
    first, pinned, weekly = sorted(db.list_rules(), key=lambda r: r["id"])
    d = datetime.strptime(first["once_date"], "%Y-%m-%d")
    assert d.weekday() == 2 and datetime(d.year, d.month, d.day, 18, tzinfo=TZ) > datetime.now(TZ)
    assert d.date() - datetime.now(TZ).date() <= timedelta(days=7)  # the *next* one
    assert pinned["once_date"] == "2026-12-09" and weekly["once_date"] is None


def test_bad_once_date_is_rejected(web):
    r = web.post("/api/rules", auth=AUTH, json={"weekday": 2, "time": "18:00", "date": "tomorrow"})
    assert r.status_code == 422


def test_cancel_registration_via_api_and_it_is_not_rebooked(web):
    from app import main
    classes = web.get("/api/schedule", auth=AUTH).json()
    target = classes[0]
    main.client._booked.add(target["id"])
    db.add_rule(target["weekday"], target["time"], "")  # a weekly rule that matches it
    day = target["start"][:10]

    assert web.post("/api/cancel", auth=AUTH, json={"class_id": target["id"], "date": day}).status_code == 200
    assert target["id"] not in main.client._booked
    assert db.get_attempt(target["id"])["status"] == "cancelled"  # so the bot leaves it alone
    after = next(c for c in web.get("/api/schedule", auth=AUTH).json() if c["id"] == target["id"])
    assert after["matched"] is False and after["registered"] is False


def test_cancel_unregistered_class_is_404(web):
    target = web.get("/api/schedule", auth=AUTH).json()[0]
    r = web.post("/api/cancel", auth=AUTH, json={"class_id": target["id"], "date": target["start"][:10]})
    assert r.status_code == 404


def test_cancel_requires_auth(web):
    assert web.post("/api/cancel", json={"class_id": "1", "date": "2026-10-20"}).status_code == 401
