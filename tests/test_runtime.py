from datetime import datetime, timezone

import pytest

from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, Event, EventType, MemoryCandidate, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class FakeModel(PersonModel):
    def react(self, context):
        self.last_request_messages = [{"role": "system", "content": "test system"}, {"role": "user", "content": context}]
        self.last_response_text = '{"action":"fake"}'
        self.last_attempt = 1
        return PersonReaction(perception="看到了", reaction="记住", mental_state_update="有点在意", action=ActionDecision(type=ActionType.REPLY, reason="自然回应", message="知道了"), memory_candidates=[MemoryCandidate(content="用户今天说到家了", memory_type="SHARED", importance=.7)])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="今天很平静。", mental_state_update="平静")


def test_runtime_writes_action_message_memory_and_trace(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, FakeModel(), "persona")
    result = runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=datetime.now(timezone.utc), content="到家了", metadata={"conversation_id": "test-conversation"}))

    assert result.reaction.action.type == ActionType.REPLY
    assert result.context
    assert result.created_memory_ids
    assert any(e.event_type == EventType.CHARACTER_MESSAGE for e in store.list_events("rin"))
    assert store.list_memories("rin")[0].content == "用户今天说到家了"

    action_event = [e for e in store.list_events("rin") if e.event_type == EventType.ACTION][-1]
    assert "trace" not in action_event.metadata
    assert action_event.metadata["trace_id"]

    trace = store.get_runtime_trace(result.event.id)
    assert trace is not None
    assert trace["source_event_id"] == result.event.id
    assert trace["conversation_id"] == "test-conversation"
    assert trace["model_messages"][1]["content"] == result.context
    assert trace["perception"] == "看到了"
    assert trace["reaction"] == "记住"
    assert trace["action"]["message"] == "知道了"
    assert trace["recalled_memories"] == []
    assert trace["created_memory_ids"] == result.created_memory_ids


class SparseModel(PersonModel):
    def react(self, context):
        self.last_request_messages = []
        self.last_response_text = '{"action":{"type":"NO_REPLY"}}'
        self.last_attempt = 1
        return PersonReaction(action=ActionDecision(type=ActionType.NO_REPLY))

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_empty_mental_state_update_keeps_previous_state(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime.now(timezone.utc)
    store.set_mental_state("rin", "原来的状态", now)
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, SparseModel(), "persona")

    result = runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=now, content="嗯"))

    assert result.reaction.mental_state_update == ""
    assert store.get_mental_state("rin") == "原来的状态"
    trace = store.get_runtime_trace(result.event.id)
    assert trace["mental_state_before"] == "原来的状态"
    assert trace["mental_state_after"] == "原来的状态"
    assert trace["mental_state_updated"] is False


class FailingCandidateEmbedding(DeterministicEmbedding):
    def embed(self, text):
        if text == "用户今天说到家了":
            raise RuntimeError("candidate embedding failed")
        return super().embed(text)


def test_derived_state_is_not_half_written_if_candidate_embedding_fails(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = FailingCandidateEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, FakeModel(), "persona")

    with pytest.raises(RuntimeError, match="candidate embedding failed"):
        runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=datetime.now(timezone.utc), content="到家了"))

    events = store.list_events("rin")
    assert [event.event_type for event in events] == [EventType.USER_MESSAGE]
    assert store.get_mental_state("rin") == ""
    assert store.list_memories("rin") == []
    assert store.list_runtime_trace_sources("rin") == set()
