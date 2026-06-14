"""Time utility helpers."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

_tz: ZoneInfo = ZoneInfo("UTC")


def configure(tz: ZoneInfo) -> None:
    """Set the engine timezone used by all helpers. Call once at startup."""
    global _tz
    _tz = tz


def now_ms() -> int:
    """Current Unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def now_sec() -> int:
    """Current Unix timestamp in seconds."""
    return int(time.time())


def ms_to_dt(ts_ms: int) -> datetime:
    """Convert a millisecond timestamp to a timezone-aware datetime."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=_tz)


def today_key() -> str:
    """Return today's date as 'YYYY-MM-DD'."""
    return datetime.now(tz=_tz).strftime("%Y-%m-%d")


def today_start_ms() -> int:
    """Unix ms for 00:00:00 today in the engine timezone."""
    d = datetime.now(tz=_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def is_stale(ts_ms: int, max_age_ms: int) -> bool:
    """True if *ts_ms* is older than *max_age_ms* milliseconds."""
    return (now_ms() - ts_ms) > max_age_ms
