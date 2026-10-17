from datetime import datetime, timedelta

from app import check, db
from app.arbox_client import ArboxClient
from tests.fake_arbox import TZ, FakeArbox, raw_class


async def test_check_reports_matches_without_booking(capsys):
    fake = FakeArbox()
    start = (datetime.now(TZ) + timedelta(days=3)).replace(hour=18, minute=0, second=0, microsecond=0)
    fake.add(raw_class(1, start, booked=3))
    fake.add(raw_class(2, start.replace(hour=19), booked=20))
    db.add_rule(start.weekday(), "18:00", "")
    db.add_rule(start.weekday(), "19:00", "")

    assert await check.main(ArboxClient(transport=fake.transport)) == 0
    out = capsys.readouterr().out
    assert "logged in" in out and "membership id = 111" in out and "2 active rule" in out
    assert "free" in out and "full -> waitlist" in out
    assert not fake.posts("/scheduleUser/insert") and not fake.posts("/scheduleStandBy/insert")


async def test_check_warns_when_no_class_matches(capsys):
    fake = FakeArbox()
    db.add_rule(1, "03:00", "")
    await check.main(ArboxClient(transport=fake.transport))
    assert "no upcoming class matches" in capsys.readouterr().out
