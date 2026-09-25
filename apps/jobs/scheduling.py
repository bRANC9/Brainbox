"""Pure schedule math: config validation, next-run computation, description.

All datetimes are timezone-aware UTC unless ``schedule_config["timezone"]``
carries an IANA zone name (the ai-handler scheduler flagged this as a future
addition; it is included here).

Config shapes (stored as JSON):
- daily:    {"hours": [1, 5], "minute": 0}
- weekly:   {"weekdays": [1, 3], "hours": [4], "minute": 0}   # 1=Mon .. 7=Sun
- monthly:  {"days_of_month": [1, 15], "hours": [4], "minute": 0}
- interval: {"every_minutes": 30}
- every one may carry {"timezone": "Europe/Budapest"}

Out-of-range values are ignored (never raise) and reported by the validator, so
a bad config can never crash the tick loop.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

logger = logging.getLogger("brainbox.jobs")

SCHEDULE_KINDS = ("interval", "daily", "weekly", "monthly")


def _as_int_list(value) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            result.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def _first_int(value, default: int) -> int:
    values = _as_int_list(value)
    return values[0] if values else default


def _zone(config: dict):
    name = (config or {}).get("timezone")
    if not name:
        return dt_timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 - bad zone must not break scheduling
        logger.warning("Unknown timezone '%s', falling back to UTC", name)
        return dt_timezone.utc


def normalize_schedule_config(kind: str, config: dict | None) -> dict:
    kind = str(kind or "").strip().lower()
    config = dict(config or {})
    zone = config.get("timezone")

    if kind == "daily":
        return {"hours": _as_int_list(config.get("hours")), "minute": _first_int(config.get("minute"), 0), "timezone": zone}
    if kind == "weekly":
        return {
            "weekdays": _as_int_list(config.get("weekdays")),
            "hours": _as_int_list(config.get("hours")),
            "minute": _first_int(config.get("minute"), 0),
            "timezone": zone,
        }
    if kind == "monthly":
        return {
            "days_of_month": _as_int_list(config.get("days_of_month")),
            "hours": _as_int_list(config.get("hours")),
            "minute": _first_int(config.get("minute"), 0),
            "timezone": zone,
        }
    if kind == "interval":
        return {"every_minutes": _first_int(config.get("every_minutes"), 0), "timezone": zone}
    return config


def validate_schedule_config(kind: str, config: dict | None) -> list[str]:
    """Return human-readable problems; an empty list means valid."""
    errors: list[str] = []
    kind = str(kind or "").strip().lower()
    config = dict(config or {})

    if kind not in SCHEDULE_KINDS:
        return [f"Unknown schedule kind '{kind}' (allowed: {', '.join(SCHEDULE_KINDS)})"]

    zone = config.get("timezone")
    if zone:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(zone))
        except Exception:  # noqa: BLE001
            errors.append(f"Unknown timezone '{zone}'.")

    minute = _first_int(config.get("minute"), 0)
    if not 0 <= minute <= 59:
        errors.append(f"Invalid minute: {minute} (0-59).")

    if kind == "interval":
        every_minutes = _first_int(config.get("every_minutes"), 0)
        if every_minutes < 1:
            errors.append("Interval schedules need 'every_minutes' >= 1.")
        return errors

    if kind == "monthly":
        days = _as_int_list(config.get("days_of_month"))
        if not days:
            errors.append("Monthly schedules need at least one 'days_of_month' entry.")
        invalid = [day for day in days if not 1 <= day <= 31]
        if invalid:
            errors.append(f"Invalid days_of_month values: {invalid} (1-31).")
    elif kind == "weekly":
        weekdays = _as_int_list(config.get("weekdays"))
        if not weekdays:
            errors.append("Weekly schedules need at least one 'weekdays' entry (1=Mon).")
        invalid = [day for day in weekdays if not 1 <= day <= 7]
        if invalid:
            errors.append(f"Invalid weekdays values: {invalid} (1-7).")

    hours = _as_int_list(config.get("hours"))
    if not hours:
        errors.append("At least one 'hours' entry is required.")
    invalid_hours = [hour for hour in hours if not 0 <= hour <= 23]
    if invalid_hours:
        errors.append(f"Invalid hours values: {invalid_hours} (0-23).")
    return errors


def compute_next_run(kind: str, config: dict | None, after: datetime | None = None) -> datetime | None:
    """First run time strictly after ``after`` (UTC), or None if unusable."""
    from django.utils import timezone as django_timezone

    kind = str(kind or "").strip().lower()
    normalized = normalize_schedule_config(kind, config)
    if after is None:
        base = django_timezone.now()
    else:
        base = after if after.tzinfo else after.replace(tzinfo=dt_timezone.utc)
    base_utc = base.astimezone(dt_timezone.utc)

    if kind == "interval":
        every_minutes = int(normalized.get("every_minutes") or 0)
        if every_minutes < 1:
            return None
        return base_utc + timedelta(minutes=every_minutes)

    zone = _zone(normalized)
    local = base_utc.astimezone(zone)
    hour_list = normalized.get("hours") or []
    minute = normalized.get("minute", 0)

    if kind == "daily":
        return _next_daily(hour_list, minute, local, zone)
    if kind == "weekly":
        return _next_weekly(normalized.get("weekdays") or [], hour_list, minute, local, zone)
    if kind == "monthly":
        return _next_monthly(normalized.get("days_of_month") or [], hour_list, minute, local, zone)
    return None


def _candidates(hours: list[int], minute: int, day: datetime, zone):
    safe_minute = minute if 0 <= minute <= 59 else 0
    for hour in hours:
        if not 0 <= hour <= 23:
            continue
        yield datetime(day.year, day.month, day.day, hour, safe_minute, tzinfo=zone)


def _next_daily(hours, minute, local, zone):
    if not hours:
        return None
    for offset in range(2):
        day = local + timedelta(days=offset)
        for candidate in _candidates(hours, minute, day, zone):
            if candidate.astimezone(dt_timezone.utc) > local.astimezone(dt_timezone.utc):
                return candidate.astimezone(dt_timezone.utc)
    return None


def _next_weekly(weekdays, hours, minute, local, zone):
    if not weekdays or not hours:
        return None
    for offset in range(8):
        day = local + timedelta(days=offset)
        if day.isoweekday() not in weekdays:
            continue
        for candidate in _candidates(hours, minute, day, zone):
            if candidate.astimezone(dt_timezone.utc) > local.astimezone(dt_timezone.utc):
                return candidate.astimezone(dt_timezone.utc)
    return None


def _next_monthly(days_of_month, hours, minute, local, zone):
    if not days_of_month or not hours:
        return None
    valid = {day for day in days_of_month if 1 <= day <= 31}
    for month_offset in range(14):
        total = local.year * 12 + (local.month - 1) + month_offset
        year, month = divmod(total, 12)
        month += 1
        last_day = calendar.monthrange(year, month)[1]
        # Clamp: a requested day missing in this month (e.g. Feb 30) runs on the
        # last real day instead, so a monthly job never skips a month.
        for day in sorted({min(value, last_day) for value in valid}):
            for candidate in _candidates(hours, minute, datetime(year, month, day, tzinfo=zone), zone):
                if candidate.astimezone(dt_timezone.utc) > local.astimezone(dt_timezone.utc):
                    return candidate.astimezone(dt_timezone.utc)
    return None


def describe_schedule(kind: str, config: dict | None) -> str:
    kind = str(kind or "").strip().lower()
    normalized = normalize_schedule_config(kind, config)
    zone_name = normalized.get("timezone")
    suffix = f" [{zone_name}]" if zone_name else ""

    def clock(hours, minute):
        return ", ".join(f"{hour:02d}:{minute:02d}" for hour in hours)

    if kind == "interval":
        minutes = int(normalized.get("every_minutes") or 0)
        if minutes >= 1440 and minutes % 1440 == 0:
            return f"every {minutes // 1440} day(s){suffix}"
        if minutes >= 60 and minutes % 60 == 0:
            return f"every {minutes // 60} hour(s){suffix}"
        return f"every {minutes} minute(s){suffix}"
    if kind == "daily":
        return f"daily at {clock(normalized.get('hours') or [], normalized.get('minute', 0))}{suffix}"
    if kind == "weekly":
        names = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
        days = ", ".join(names.get(day, str(day)) for day in normalized.get("weekdays") or [])
        return f"weekly on {days} at {clock(normalized.get('hours') or [], normalized.get('minute', 0))}{suffix}"
    if kind == "monthly":
        days = ", ".join(f"{day}." for day in normalized.get("days_of_month") or [])
        return f"monthly on {days} at {clock(normalized.get('hours') or [], normalized.get('minute', 0))}{suffix}"
    return f"{kind}: {config}{suffix}"
