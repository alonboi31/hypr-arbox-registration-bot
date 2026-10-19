from app import db


def test_rule_crud():
    assert db.list_rules() == []
    db.add_rule(2, "18:00", "  hall a ")
    db.add_rule(0, "07:00")
    rules = db.list_rules()
    assert [(r["weekday"], r["time"], r["name_contains"], r["enabled"]) for r in rules] == [
        (0, "07:00", "", 1), (2, "18:00", "hall a", 1)]  # sorted, trimmed
    rid = rules[0]["id"]
    db.set_rule_enabled(rid, False)
    assert db.list_rules()[0]["enabled"] == 0
    db.delete_rule(rid)
    assert len(db.list_rules()) == 1


def test_claim_attempt_is_only_granted_once():
    assert db.claim_attempt("c1", "WOD") is True
    assert db.claim_attempt("c1", "WOD") is False
    assert db.get_attempt("c1")["status"] == "pending"


def test_attempt_status_updates():
    db.claim_attempt("c2", "HYROX")
    db.set_attempt("c2", "failed", "nope")
    a = db.get_attempt("c2")
    assert (a["status"], a["detail"], a["class_label"]) == ("failed", "nope", "HYROX")
    assert db.get_attempt("missing") is None


def test_recent_attempts_limit():
    for i in range(5):
        db.claim_attempt(f"x{i}", f"L{i}")
    assert len(db.recent_attempts(limit=3)) == 3
    assert len(db.recent_attempts()) == 5


def test_once_rule_roundtrip_and_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    from dataclasses import replace
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)  # a database made before one-time rules existed
    old.execute("CREATE TABLE rules (id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER NOT NULL, time TEXT NOT NULL,"
                " name_contains TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1)")
    old.execute("INSERT INTO rules (weekday, time) VALUES (1, '18:00')")
    old.commit(); old.close()
    monkeypatch.setattr(db, "settings", replace(db.settings, db_path=path))
    db.init()
    assert db.list_rules()[0]["once_date"] is None  # existing rule survives, as a weekly rule
    db.add_rule(2, "07:00", "wod", "2026-10-21")
    assert [r["once_date"] for r in db.list_rules()] == [None, "2026-10-21"]


def test_expire_and_finish_once_rules():
    db.add_rule(0, "18:00", "", "2026-10-01")
    db.add_rule(0, "19:00", "", "2026-12-01")
    db.add_rule(0, "20:00", "")  # weekly: never expires
    db.expire_once_rules("2026-10-19")
    assert [r["enabled"] for r in db.list_rules()] == [0, 1, 1]
    db.finish_once_rules([db.list_rules()[1]["id"]])
    assert [r["enabled"] for r in db.list_rules()] == [0, 0, 1]
