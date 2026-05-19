"""Africa/Addis_Ababa timezone utilities."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ADDIS_TZ = ZoneInfo("Africa/Addis_Ababa")


def now_addis() -> datetime:
    """Current time in Addis Ababa."""
    return datetime.now(ADDIS_TZ)


def to_addis(dt: datetime) -> datetime:
    """Convert a datetime to Addis Ababa timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ADDIS_TZ)
    return dt.astimezone(ADDIS_TZ)


def format_datetime(dt: datetime) -> str:
    """Format a datetime for display: 'May 22, 2026 — 11:59 PM'."""
    addis_dt = to_addis(dt)
    return addis_dt.strftime("%B %d, %Y — %I:%M %p")


def reminder_times(deadline: datetime, offsets_hours: list[int]) -> list[datetime]:
    """Generate reminder datetimes from a deadline and hour offsets.

    Args:
        deadline: The event deadline.
        offsets_hours: Hours before deadline (e.g. [48, 24, 0]).

    Returns:
        List of reminder datetimes, filtered to future only.
    """
    current = now_addis()
    times = []
    for hours in offsets_hours:
        remind_at = to_addis(deadline) - timedelta(hours=hours)
        if remind_at > current:
            times.append(remind_at)
    return times
