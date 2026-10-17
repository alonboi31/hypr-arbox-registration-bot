from datetime import datetime, timedelta

from .config import settings


def opening_time(start: datetime) -> datetime:
    """When registration opens for a class starting at `start` (tz-aware).

    Sunday/Monday classes open on the preceding Thursday at the same clock
    time; everything else opens 48h before the start.
    """
    if start.weekday() in settings.early_open_weekdays:
        days_back = (start.weekday() - 3) % 7  # Thursday == 3
        # Sunday(6) -> 3 days back, Monday(0) -> 4 days back
        return start - timedelta(days=days_back)
    return start - timedelta(hours=settings.default_open_hours)
