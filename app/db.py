import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,          -- Mon=0 .. Sun=6
    time TEXT NOT NULL,                -- HH:MM local
    name_contains TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attempts (
    class_id TEXT PRIMARY KEY,
    class_label TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | booked | waitlisted | failed
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def conn():
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def list_rules():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rules ORDER BY weekday, time")]


def add_rule(weekday: int, time: str, name_contains: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO rules (weekday, time, name_contains) VALUES (?,?,?)",
            (weekday, time, name_contains.strip()),
        )


def set_rule_enabled(rule_id: int, enabled: bool):
    with conn() as c:
        c.execute("UPDATE rules SET enabled=? WHERE id=?", (int(enabled), rule_id))


def delete_rule(rule_id: int):
    with conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def get_attempt(class_id: str):
    with conn() as c:
        r = c.execute("SELECT * FROM attempts WHERE class_id=?", (class_id,)).fetchone()
        return dict(r) if r else None


def claim_attempt(class_id: str, label: str) -> bool:
    """Atomically reserve a class so it is only processed once."""
    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO attempts (class_id, class_label, status) VALUES (?,?, 'pending')",
            (class_id, label),
        )
        return cur.rowcount == 1


def set_attempt(class_id: str, status: str, detail: str = ""):
    with conn() as c:
        c.execute(
            "UPDATE attempts SET status=?, detail=?, updated_at=CURRENT_TIMESTAMP WHERE class_id=?",
            (status, detail, class_id),
        )


def recent_attempts(limit: int = 30):
    with conn() as c:
        return [
            dict(r)
            for r in c.execute("SELECT * FROM attempts ORDER BY updated_at DESC LIMIT ?", (limit,))
        ]


def release_dry_runs():
    """Forget rehearsal results so those classes can be booked for real."""
    with conn() as c:
        c.execute("DELETE FROM attempts WHERE status='dry-run'")
