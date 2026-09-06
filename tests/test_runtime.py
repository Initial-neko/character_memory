from datetime import datetime, timezone

from character_memory.domain.models import *
from character_memory.storage.sqlite import SQLiteStore
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.llm.client import PersonModel


class FakeModel(PersonModel):
    def react(self, context):
        self.last_request_messages = [
            {"role": "system", "content": "test system"},
            {"role": "user", "content": context},
        ]
        self.last_response_text = '{"action":"fake"}'
        self.last_attempt = 1
        return PersonReaction(
            perception="看到了",
            reaction="记住",
            mental_state_update="有点在意",
            action=ActionDecision(type=ActionType.REPLY, reason="自然回应", message="知道了"),
            memory_candidates=[
                MemoryCandidate(content="用户今天说到家了", memory_type="SHARED", importance=.7)
            ],
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="今天很平静。", mental_state_update="平静")


def test_runtime_writes_action_message_memory_and_trace(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, FakeModel(), "persona")
    result = runtime.handle(
        Event(
            character_id="rin",
            event_type=EventType.USER_MESSAGE,
            event_time=datetime.now(timezone.utc),
            content="到家了",
        )
    )

    assert result.reaction.action.type == ActionType.REPLY
    assert result.context
    assert result.created_memory_ids
    assert any(e.event_type == EventType.CHARACTER_MESSAGE for e in store.list_events("rin"))
    assert store.list_memories("rin")[0].content == "用户今天说到家了"

    action_event = [e for e in store.list_events("rin") if e.event_type == EventType.ACTION][-1]
    trace = action_event.metadata["trace"]
    assert trace["source_event_id"] == result.event.id
    assert trace["model_messages"][1]["content"] == result.context
    assert trace["perception"] == "看到了"
    assert trace["reaction"] == "记住"
    assert trace["action"]["message"] == "知道了"
    assert trace["recalled_memories"] == []
    assert trace["created_memory_ids"] == result.created_memory_ids
