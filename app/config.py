import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    arbox_email: str = os.getenv("ARBOX_EMAIL", "")
    arbox_password: str = os.getenv("ARBOX_PASSWORD", "")
    # TODO: fill in after the traffic capture (see README).
    arbox_base_url: str = os.getenv("ARBOX_BASE_URL", "https://CHANGE-ME.example")
    timezone: str = os.getenv("TZ", "Asia/Jerusalem")
    ui_user: str = os.getenv("UI_USER", "admin")
    ui_password: str = os.getenv("UI_PASSWORD", "")
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    ntfy_topic: str = os.getenv("NTFY_TOPIC", "")
    mock: bool = os.getenv("ARBOX_MOCK", "0") == "1"
    # Classes on these weekdays (Mon=0 .. Sun=6) open on the previous Thursday
    # at the class's clock time; all others open `default_open_hours` before.
    early_open_weekdays: tuple = (6, 0)
    default_open_hours: int = 48
    lookahead_days: int = 5


settings = Settings()
