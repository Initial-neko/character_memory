"""Conservative DeepSeek reference-window pacing for background opportunities.

This module decides *when* to offer autonomous work, never what a provider
actually bills. In particular, a proxy's price may be flat or quota-based.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
PEAK = "PEAK"
OFF_PEAK = "OFF_PEAK"


def reference_billing_window(now: datetime, holidays=()) -> str:
    """Classify a timezone-aware instant; configured dates are Beijing-local.

    DeepSeek's weekday windows are [09:00, 12:00) and [14:00, 18:00).
    Statutory holidays must be maintained explicitly in settings. A workday
    adjustment falling on a weekend remains off-peak under the weekday rule.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reference billing window requires an aware datetime")
    local = now.astimezone(BEIJING)
    if local.weekday() >= 5 or local.date().isoformat() in holidays:
        return OFF_PEAK
    clock = local.time()
    if time(9) <= clock < time(12) or time(14) <= clock < time(18):
        return PEAK
    return OFF_PEAK


def paced_interval_minutes(settings, base_minutes: float, *, now: datetime) -> float:
    """Pace background Space/World opportunity intervals, with a safe minimum.

    No implicit discount is applied to token usage, and an existing durable
    next-run deadline is not silently rewritten when a window changes.
    """
    if not bool(getattr(settings, "activity_pacing_enabled", False)):
        return base_minutes
    window = reference_billing_window(
        now, getattr(settings, "activity_reference_holidays", ())
    )
    attr = (
        "activity_peak_interval_multiplier"
        if window == PEAK else "activity_offpeak_interval_multiplier"
    )
    multiplier = float(getattr(settings, attr, 1.0))
    return max(10.0, min(10080.0, base_minutes * multiplier))
