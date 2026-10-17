"""Calculate the next permitted rule time without waking a model."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _boundary(day: date, clock: time, zone: ZoneInfo, *, latest: bool) -> datetime:
    wall = datetime.combine(day, clock)
    candidates = [wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [instant for instant in candidates if instant.astimezone(zone).replace(tzinfo=None) == wall]
    # During a skipped hour, use its later normalized instant. During a
    # repeated hour, keep the quiet interval continuous through the last end.
    return max(valid or candidates) if latest or not valid else min(valid)


def quiet_until(hours: dict[str, str] | None, now: datetime, timezone: str) -> datetime | None:
    if hours is None:
        return None
    if now.utcoffset() is None:
        raise ValueError("quiet-hours clock must include a timezone")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    start, end = time.fromisoformat(hours["start"]), time.fromisoformat(hours["end"])
    for day in (local.date() - timedelta(days=1), local.date()):
        first = _boundary(day, start, zone, latest=False)
        last = _boundary(day + timedelta(days=int(start > end)), end, zone, latest=True)
        if first <= now < last:
            return last
    return None
