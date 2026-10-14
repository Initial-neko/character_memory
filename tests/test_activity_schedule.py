from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from character_memory.activity_schedule import (
    OFF_PEAK,
    PEAK,
    paced_interval_minutes,
    reference_billing_window,
)
from character_memory.config import Settings
from character_memory.space_autonomy import SpaceAutonomyScheduler
from character_memory.world_activity import WorldActivityScheduler


def stamp(day: int, hour: int, minute: int = 0, *, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (stamp(13, 0, 59), OFF_PEAK),  # 08:59 Beijing
        (stamp(13, 1), PEAK),         # 09:00
        (stamp(13, 3, 59), PEAK),     # 11:59
        (stamp(13, 4), OFF_PEAK),     # 12:00
        (stamp(13, 5, 59), OFF_PEAK), # 13:59
        (stamp(13, 6), PEAK),         # 14:00
        (stamp(13, 9, 59), PEAK),     # 17:59
        (stamp(13, 10), OFF_PEAK),    # 18:00
        (stamp(17, 2), OFF_PEAK),     # Saturday 10:00
        (stamp(18, 2), OFF_PEAK),     # Sunday 10:00
    ],
)
def test_reference_window_has_exact_beijing_boundaries(instant, expected):
    assert reference_billing_window(instant) == expected


def test_holiday_override_and_no_timezone_guessing():
    assert reference_billing_window(stamp(14, 2)) == PEAK
    assert reference_billing_window(stamp(14, 2), ["2026-10-14"]) == OFF_PEAK
    with pytest.raises(ValueError, match="aware"):
        reference_billing_window(datetime(2026, 10, 13, 9, 0))


def test_holidays_are_validated_in_settings():
    assert Settings(activity_reference_holidays=["2026-10-14", "2026-10-14"]).activity_reference_holidays == ["2026-10-14"]
    with pytest.raises(ValidationError):
        Settings(activity_reference_holidays=["2026-02-30"])
    with pytest.raises(ValidationError):
        Settings(activity_reference_holidays=["next holiday"])


def test_policy_is_opt_in_and_clamps_interval():
    default = Settings()
    assert paced_interval_minutes(default, 60, now=stamp(13, 1)) == 60
    enabled = Settings(activity_pacing_enabled=True)
    assert paced_interval_minutes(enabled, 60, now=stamp(13, 1)) == 120
    assert paced_interval_minutes(enabled, 60, now=stamp(13, 4)) == 30
    assert paced_interval_minutes(enabled, 10, now=stamp(13, 4)) == 10
    assert paced_interval_minutes(enabled, 10080, now=stamp(13, 1)) == 10080


def test_existing_scheduler_intervals_use_same_policy_without_paid_call():
    settings = Settings(
        activity_pacing_enabled=True,
        space_opportunity_interval_minutes=120,
        world_browse_interval_minutes=60,
        world_pulse_refresh_minutes=60,
    )
    space = object.__new__(SpaceAutonomyScheduler)
    space.access = SimpleNamespace(settings=settings)
    world = object.__new__(WorldActivityScheduler)
    world.access = SimpleNamespace(settings=settings)
    assert space.interval_minutes(stamp(13, 1)) == 240
    assert space.interval_minutes(stamp(13, 4)) == 60
    assert world._interval("world_browse_interval_minutes", 30, stamp(13, 1)) == 120
    assert world._interval("world_browse_interval_minutes", 30, stamp(13, 4)) == 30
    assert world._interval("world_pulse_refresh_minutes", 60, stamp(13, 1)) == 120
    settings.world_cost_saving_enabled = True
    assert world._interval("world_browse_interval_minutes", 30, stamp(13, 1)) == 240
    assert world._interval("world_browse_interval_minutes", 30, stamp(13, 4)) == 60
