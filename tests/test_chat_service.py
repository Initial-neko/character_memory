from datetime import datetime, timezone

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class SessionAwareFake(PersonModel):
    def __init__(self):
        self.session = None

    def react(self, context):
        return self._reaction()

    def react_for_session(self, context, session_id):
        self.session = session_id
        return self._reaction()

    @staticmethod
    def _reaction():
        return PersonReaction(perception="收到", reaction="正常回应", mental_state_update="平静", action=ActionDecision(type=ActionType.REPLY, reason="自然回应", message="你好"))

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="平静。", mental_state_update="平静")


def test_chat_service_owns_clock_and_conversation_session(tmp_path):
    fixed = datetime(2026, 9, 7, 21, 30, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = SessionAwareFake()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona")
    service = ChatService(store, runtime, FixedClock(fixed))

    result = service.send("你好", character_id="rin", conversation_id="browser-session")

    assert result.event.event_time == fixed
    assert store.get_world_time("rin") == fixed
    assert model.session == "browser-session"
    history = service.history("rin")
    assert [item["role"] for item in history["messages"]] == ["user", "assistant"]
    assert all(item["has_trace"] for item in history["messages"])
