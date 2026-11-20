"""Time helpers (CONTRACT §2): fixed-offset time zones, ISO-8601 with offset,
local dates and period boundaries. No zoneinfo: only DST-free zones are used.
"""

from __future__ import annotations

import datetime as _dt
import re

# CONTRACT §2: allowed company time zones and their fixed offsets.
TIMEZONES: dict[str, str] = {
    "Asia/Almaty": "+05:00",
    "Asia/Dubai": "+04:00",
    "Asia/Kolkata": "+05:30",
    "Asia/Tokyo": "+09:00",
    "America/Bogota": "-05:00",
    "Etc/UTC": "+00:00",
}

UTC_OFFSET = "+00:00"
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")


def offset_minutes(offset: str) -> int:
    m = _OFFSET_RE.match(offset)
    if not m:
        raise ValueError(f"bad utc offset {offset!r}")
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))


def tzinfo(offset: str) -> _dt.timezone:
    return _dt.timezone(_dt.timedelta(minutes=offset_minutes(offset)))


def offset_for(timezone_name: str) -> str:
    try:
        return TIMEZONES[timezone_name]
    except KeyError:
        raise ValueError(f"unsupported timezone {timezone_name!r}") from None


def parse_ts(value: str) -> _dt.datetime:
    """Parse ISO-8601 with an explicit ``±HH:MM`` offset (``Z`` is rejected)."""
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise ValueError(f"timestamp must be ISO-8601 with offset: {value!r}")
    return _dt.datetime.fromisoformat(value)


def format_ts(dt: _dt.datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS±HH:MM`` (seconds precision, offset always shown)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.isoformat(timespec="seconds")


def as_ts(value: "str | _dt.datetime") -> _dt.datetime:
    return parse_ts(value) if isinstance(value, str) else value


def to_offset(value: "str | _dt.datetime", offset: str) -> _dt.datetime:
    return as_ts(value).astimezone(tzinfo(offset))


def reformat(value: "str | _dt.datetime", offset: str) -> str:
    """Same instant, expressed in ``offset``."""
    return format_ts(to_offset(value, offset))


def local_date(value: "str | _dt.datetime", offset: str) -> _dt.date:
    """Date of the instant in the company offset (CONTRACT §2)."""
    return to_offset(value, offset).date()


def utc_date(value: "str | _dt.datetime") -> _dt.date:
    return to_offset(value, UTC_OFFSET).date()


def local_datetime(d: _dt.date, minute_of_day: int, offset: str, second: int = 0) -> _dt.datetime:
    """Aware datetime at local ``d`` + ``minute_of_day`` minutes in ``offset``."""
    if not 0 <= minute_of_day < 24 * 60:
        raise ValueError("minute_of_day out of range")
    return _dt.datetime(d.year, d.month, d.day, minute_of_day // 60, minute_of_day % 60,
                        second, tzinfo=tzinfo(offset))


def local_midnight(d: _dt.date, offset: str) -> _dt.datetime:
    return local_datetime(d, 0, offset)


def parse_month(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})", value) if isinstance(value, str) else None
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"start_month must be YYYY-MM: {value!r}")
    return int(m.group(1)), int(m.group(2))


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def period_bounds(start_month: str, months: int, offset: str) -> tuple[_dt.datetime, _dt.datetime]:
    """``[period_start, period_end)`` as local midnights in ``offset``."""
    y, m = parse_month(start_month)
    ey, em = add_months(y, m, months)
    return (local_midnight(_dt.date(y, m, 1), offset),
            local_midnight(_dt.date(ey, em, 1), offset))


def in_range(value: "str | _dt.datetime", start: _dt.datetime, end: _dt.datetime) -> bool:
    """``start <= value < end`` compared as instants."""
    return start <= as_ts(value) < end


def date_range(start: _dt.date, end_exclusive: _dt.date) -> list[_dt.date]:
    out = []
    d = start
    while d < end_exclusive:
        out.append(d)
        d += _dt.timedelta(days=1)
    return out
