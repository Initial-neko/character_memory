from __future__ import annotations

from datetime import datetime, time, timedelta


class DayRunner:
    """Persistent virtual-time runner for one character."""

    def __init__(self, store, life, ticker):
        self.store = store
        self.life = life
        self.ticker = ticker

    def current_time(self, character_id: str) -> datetime:
        current = self.store.get_world_time(character_id)
        if current is None:
            current = datetime.now().astimezone()
            self.store.set_world_time(character_id, current)
        return current

    @staticmethod
    def _day_start(base: datetime, days_ahead: int = 1) -> datetime:
        target = (base + timedelta(days=days_ahead)).date()
        return datetime.combine(target, time.min, tzinfo=base.tzinfo)

    def run_next_day(self, character_id: str, tick_hours: tuple[int, ...] = (9, 15, 20)):
        current = self.current_time(character_id)
        day = self._day_start(current, 1)
        self.store.set_world_time(character_id, day)
        life_events = self.life.simulate_day(character_id, day)
        tick_results = []
        for hour in tick_hours:
            t = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            self.store.set_world_time(character_id, t)
            tick_results.extend(self.ticker.tick(character_id, t))
        diary = self.life.end_day(character_id, day)
        next_start = self._day_start(day, 1)
        self.store.set_world_time(character_id, next_start)
        proactive = sum(1 for r in tick_results if r.reaction.action.message)
        return {
            "date": day.date().isoformat(),
            "life_events": len(life_events),
            "ticks": len(tick_results),
            "proactive_messages": proactive,
            "diary": diary.diary,
        }

    def simulate(self, character_id: str, days: int, tick_hours: tuple[int, ...] = (9, 15, 20)):
        return [self.run_next_day(character_id, tick_hours=tick_hours) for _ in range(days)]
