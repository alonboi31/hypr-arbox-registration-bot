from datetime import datetime
from zoneinfo import ZoneInfo

from app.engine import matches
from app.arbox_client import GymClass
from app.opening import opening_time

TZ = ZoneInfo("Asia/Jerusalem")


def dt(y, m, d, h=18, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def test_sunday_opens_previous_thursday_same_time():
    assert opening_time(dt(2026, 10, 18)) == dt(2026, 10, 15)  # Sun -> Thu


def test_monday_opens_previous_thursday_same_time():
    assert opening_time(dt(2026, 10, 19, 7)) == dt(2026, 10, 15, 7)  # Mon -> Thu


def test_other_days_open_48h_before():
    assert opening_time(dt(2026, 10, 21)) == dt(2026, 10, 19)  # Wed -> Mon


def test_rule_matching():
    c = GymClass("1", "WOD", dt(2026, 10, 21))  # Wednesday
    rule = {"enabled": 1, "weekday": 2, "time": "18:00", "name_contains": "wod"}
    assert matches(rule, c)
    assert not matches({**rule, "time": "19:00"}, c)
    assert not matches({**rule, "name_contains": "yoga"}, c)
    assert not matches({**rule, "enabled": 0}, c)
