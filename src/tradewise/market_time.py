"""Indian market clock (IST, no daylight saving). Exchange holidays are not listed: on a holiday
no prices are published, and the updater simply stops retrying for that day."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving
CLOSE_READY = time(16, 0)  # market closes 3:30 pm; daily data is final by about 4 pm


def now_ist() -> datetime:
    return datetime.now(IST)


def last_complete_session(now: datetime | None = None) -> pd.Timestamp:
    """The latest weekday whose closing prices should be available (ignores exchange holidays)."""
    now = (now or now_ist()).astimezone(IST)
    d = now.date()
    if now.weekday() >= 5 or now.time() < CLOSE_READY:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return pd.Timestamp(d)


def next_close_check(now: datetime | None = None) -> datetime:
    """When the next day's closing prices will be ready."""
    now = (now or now_ist()).astimezone(IST)
    d = now.date()
    if now.weekday() >= 5 or now.time() >= CLOSE_READY:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, CLOSE_READY, tzinfo=IST)
