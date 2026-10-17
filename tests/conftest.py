import os
import tempfile

# Must be set before the app modules are imported (settings are read at import).
os.environ.update(
    ARBOX_MOCK="1",
    UI_PASSWORD="pw",
    UI_USER="admin",
    ARBOX_EMAIL="me@example.com",
    ARBOX_PASSWORD="secret",
    TZ="Asia/Jerusalem",
    DB_PATH=os.path.join(tempfile.mkdtemp(), "boot.db"),
    NTFY_TOPIC="",
)

from dataclasses import replace  # noqa: E402

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite database."""
    monkeypatch.setattr(db, "settings", replace(db.settings, db_path=str(tmp_path / "t.db")))
    db.init()
