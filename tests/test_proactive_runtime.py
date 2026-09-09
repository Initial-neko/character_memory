from datetime import datetime, timedelta, timezone

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class ProactiveFakeModel(PersonModel):
    def react(self, context):
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="那个汇报结束了吗？")])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_proactive_turn_reuses_latest_conversation_and_marks_message_source(tmp_path):
    t0 = datetime(2026, 9, 9, 9, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, ProactiveFakeModel(), "persona")
    service = ChatService(store, {"momo": runtime}, FixedClock(t0))

    service.send("下午要汇报", character_id="momo", conversation_id="browser-session", at=t0)
    result = service.dispatch_proactive_intent(
        character_id="momo",
        intent_id=7,
        content="下午之后问问汇报结果",
        at=t0 + timedelta(hours=5),
    )

    source = result.event
    assert source.event_type == EventType.PROACTIVE_INTENT
    assert source.metadata["intent_id"] == 7
    assert source.metadata["conversation_id"] == "browser-session"

    messages = store.list_events("momo", limit=10, event_type=EventType.CHARACTER_MESSAGE.value)
    proactive = messages[-1]
    assert proactive.content == "那个汇报结束了吗？"
    assert proactive.metadata["source_event_id"] == source.id
    assert proactive.metadata["source_event_type"] == EventType.PROACTIVE_INTENT.value
    assert proactive.metadata["conversation_id"] == "browser-session"
    store.close()
