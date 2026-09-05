from datetime import datetime
from character_memory.life.clock import WorldClock
def test_clock_advance():
    c=WorldClock(datetime(2026,9,5,8)); c.advance(hours=2); assert c.current_time.hour==10
