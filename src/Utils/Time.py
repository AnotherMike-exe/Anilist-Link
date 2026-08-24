"""Timezone helpers for rendering stored timestamps.

Every timestamp in the database is written in **UTC** — SQLite's
``datetime('now')`` / ``CURRENT_TIMESTAMP`` defaults and the
``datetime.now(timezone.utc)`` calls in the sync code all produce UTC. The UI
must therefore convert before displaying, otherwise a job that ran at 02:00
local time is rendered as 09:00, a time that has not happened yet.

The display timezone comes from the ``TZ`` environment variable (the Binhex
standard variable, e.g. ``America/Los_Angeles``). If ``TZ`` is unset or names
a zone the container does not have, we fall back to the process's local time
and finally to UTC.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

_warned_zones: set[str] = set()


def get_timezone(name: str | None = None) -> tzinfo:
    """Return the timezone timestamps should be displayed in.

    ``name`` overrides the ``TZ`` environment variable when given.
    """
    tz_name = (name or os.environ.get("TZ") or "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            if tz_name not in _warned_zones:
                _warned_zones.add(tz_name)
                logger.warning(
                    "Unknown timezone %r (is tzdata installed?) — "
                    "falling back to system local time",
                    tz_name,
                )
    # ``astimezone()`` on an aware datetime resolves the system local zone.
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc


def parse_utc(value: str | datetime | None) -> datetime | None:
    """Parse a stored timestamp into a timezone-aware UTC datetime.

    Handles the SQLite ``YYYY-MM-DD HH:MM:SS`` form, ISO-8601 strings with a
    ``T`` separator, fractional seconds, and trailing ``Z``/offsets. Naive
    values are assumed to be UTC, which is how they are written. Returns
    ``None`` if the value cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:19], DATETIME_FORMAT)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(
    value: str | datetime | None,
    fmt: str = DATETIME_FORMAT,
    tz_name: str | None = None,
) -> str:
    """Format a stored UTC timestamp in the configured display timezone.

    Unparseable input is returned unchanged so a malformed value is visible in
    the UI rather than silently blanked.
    """
    parsed = parse_utc(value)
    if parsed is None:
        return "" if value is None else str(value)
    return parsed.astimezone(get_timezone(tz_name)).strftime(fmt)


def to_local_date(value: str | datetime | None, tz_name: str | None = None) -> str:
    """Format a stored UTC timestamp as a local ``YYYY-MM-DD`` date."""
    return to_local(value, fmt=DATE_FORMAT, tz_name=tz_name)


def now_local(tz_name: str | None = None) -> datetime:
    """Return the current time in the configured display timezone."""
    return datetime.now(get_timezone(tz_name))
