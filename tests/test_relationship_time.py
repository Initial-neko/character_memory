from datetime import datetime, timezone

from character_memory.domain.models import DailyLifePlan, DiaryResult, Event, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class CaptureModel(PersonModel):
    def __init__(self):
        self.context = ""

    def react(self, context):
        self.context = context
        return PersonReaction(actions=[])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_relationship_time_ignores_future_chat_events(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = CaptureModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona")

    day1 = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    day5 = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    day10 = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)
    store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=day1, content="过去的聊天"))
    store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=day10, content="未来的聊天"))

    runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=day5, content="回放到这里"))

    assert "距离上次聊天：4 天" in model.context
    assert day1.isoformat() in model.context
    assert day10.isoformat() not in model.context
