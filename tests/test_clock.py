from datetime import datetime, timezone

from character_memory.application.clock import FixedClock


def test_fixed_clock_returns_injected_time():
    value = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    clock = FixedClock(value)
    assert clock.now() == value
