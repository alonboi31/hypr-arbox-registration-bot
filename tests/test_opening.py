from datetime import datetime
from zoneinfo import ZoneInfo

from app.arbox_client import GymClass, _parse_class
from app.engine import matches
from app.opening import opening_time

TZ = ZoneInfo("Asia/Jerusalem")


def dt(y, m, d, h=18, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def test_sunday_72h_is_thursday_same_time():
    assert opening_time(dt(2026, 10, 18, 13), 72) == dt(2026, 10, 15, 13)


def test_default_is_48h():
    assert opening_time(dt(2026, 10, 21)) == dt(2026, 10, 19)
    assert opening_time(dt(2026, 10, 21), 48) == dt(2026, 10, 19)


def test_rule_matching():
    c = GymClass("1", "W.O.D Hall A", dt(2026, 10, 21))  # Wednesday
    rule = {"enabled": 1, "weekday": 2, "time": "18:00", "name_contains": "wod"}
    assert not matches(rule, c)  # "wod" is not a substring of "W.O.D Hall A"
    assert matches({**rule, "name_contains": "hall a"}, c)
    assert matches({**rule, "name_contains": ""}, c)
    assert not matches({**rule, "time": "19:00"}, c)
    assert not matches({**rule, "enabled": 0}, c)


def test_parse_class_from_captured_shape():
    raw = {
        "id": 54858289, "enable_registration_time": 72, "max_users": 20, "free": 14,
        "user_booked": None, "user_in_standby": None, "booked_users": [{}] * 6,
        "date_time": {"date": "2026-10-18 13:00:00", "timezone": "Asia/Jerusalem"},
        "box_categories": {"name": "W.O.D Hall A"},
    }
    c = _parse_class(raw)
    assert (c.id, c.name, c.start, c.registration_hours) == ("54858289", "W.O.D Hall A", dt(2026, 10, 18, 13), 72)
    assert not c.full and not c.registered
    assert _parse_class({**raw, "free": 0}).full
    assert _parse_class({**raw, "user_booked": 192866245}).registered
