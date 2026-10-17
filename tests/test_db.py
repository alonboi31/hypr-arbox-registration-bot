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
