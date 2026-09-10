from __future__ import annotations

from datetime import datetime, timezone


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def aware_datetime(value: datetime) -> datetime:
    """Return an aware datetime; legacy naive values are interpreted as UTC.

    Earlier Character Memory code already treated naive timestamps as UTC when
    comparing in Python, so the migration preserves that compatibility rule.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def epoch_us(value: datetime) -> int:
    """Stable absolute-time key used by SQLite comparisons and ordering."""

    utc = aware_datetime(value).astimezone(timezone.utc)
    delta = utc - _EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds


def parse_datetime(value: str) -> datetime:
    return aware_datetime(datetime.fromisoformat(value))


def epoch_us_from_iso(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    return epoch_us(parse_datetime(str(value)))
