from datetime import datetime, timedelta

from .config import settings


def opening_time(start: datetime, registration_hours: int | None = None) -> datetime:
    """When registration opens for a class starting at `start` (tz-aware).

    Arbox sends each class's `enable_registration_time` (hours before start):
    48 for most classes, 72 for Sunday and most Monday classes.
    """
    hours = registration_hours if registration_hours is not None else settings.default_open_hours
    return start - timedelta(hours=hours)
