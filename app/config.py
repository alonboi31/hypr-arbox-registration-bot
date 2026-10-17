import os
from dataclasses import dataclass


def _int(name: str, default: int | None) -> int | None:
    v = os.getenv(name)
    return int(v) if v else default


@dataclass(frozen=True)
class Settings:
    arbox_email: str = os.getenv("ARBOX_EMAIL", "")
    arbox_password: str = os.getenv("ARBOX_PASSWORD", "")
    arbox_base_url: str = os.getenv("ARBOX_BASE_URL", "https://apiappv2.arboxapp.com/api/v2")
    whitelabel: str = os.getenv("ARBOX_WHITELABEL", "HYPR-training")
    # Studio ids from the app's traffic (locations_box_id / boxes_id).
    location_id: int = _int("ARBOX_LOCATION_ID", 48)
    box_id: int = _int("ARBOX_BOX_ID", 59)
    # Optional: pin which membership to book with (default: auto-pick active plan).
    membership_user_id: int | None = _int("ARBOX_MEMBERSHIP_USER_ID", None)
    timezone: str = os.getenv("TZ", "Asia/Jerusalem")
    ui_user: str = os.getenv("UI_USER", "admin")
    ui_password: str = os.getenv("UI_PASSWORD", "")
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    ntfy_topic: str = os.getenv("NTFY_TOPIC", "")
    mock: bool = os.getenv("ARBOX_MOCK", "0") == "1"
    # Log what would be booked without sending the booking request.
    dry_run: bool = os.getenv("DRY_RUN", "0") == "1"
    # Fallback only: the server tells us each class's opening window.
    default_open_hours: int = 48
    lookahead_days: int = 5


settings = Settings()
