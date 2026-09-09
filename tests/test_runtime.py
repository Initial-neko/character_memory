from datetime import datetime, timedelta, timezone

import pytest

from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, Event, EventType, Memory, MemoryCandidate, PersonReaction
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
    assert len(result.reaction.actions) == 1
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
    assert trace["actions"][0]["message"] == "知道了"
    assert trace["memory_decisions"][0]["decision"] == "WRITE"
    assert trace["recalled_memories"] == []
    assert trace["created_memory_ids"] == result.created_memory_ids


class SparseModel(PersonModel):
    def __init__(self):
        self.contexts = []

    def react(self, context):
        self.contexts.append(context)
        self.last_request_messages = []
        self.last_response_text = '{"actions":[]}'
        self.last_attempt = 1
        return PersonReaction(actions=[])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_empty_actions_are_real_silence_and_keep_previous_state(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime.now(timezone.utc)
    store.set_mental_state("rin", "原来的状态", now)
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, SparseModel(), "persona")

    result = runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=now, content="嗯"))

    assert result.reaction.actions == []
    assert result.reaction.action.type == ActionType.NO_REPLY
    assert result.reaction.mental_state_update == ""
    assert store.get_mental_state("rin") == "原来的状态"
    assert not any(event.event_type == EventType.CHARACTER_MESSAGE for event in store.list_events("rin"))
    trace = store.get_runtime_trace(result.event.id)
    assert trace["actions"] == []
    assert trace["mental_state_before"] == "原来的状态"
    assert trace["mental_state_after"] == "原来的状态"
    assert trace["mental_state_updated"] is False


class MultiActionModel(SparseModel):
    def react(self, context):
        self.contexts.append(context)
        self.last_request_messages = []
        self.last_response_text = '{"actions":[...]}'
        self.last_attempt = 1
        return PersonReaction(actions=[
            ActionDecision(type=ActionType.MESSAGE, message="等下等下"),
            ActionDecision(type=ActionType.EMOJI, message="🥺"),
            ActionDecision(type=ActionType.MESSAGE, message="怎么回事呀？"),
        ])


def test_runtime_persists_up_to_three_visible_actions_in_order(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, MultiActionModel(), "persona")
    result = runtime.handle(Event(character_id="momo", event_type=EventType.USER_MESSAGE, event_time=datetime.now(timezone.utc), content="今天被老板骂了"))

    assert [action.type for action in result.reaction.actions] == [ActionType.MESSAGE, ActionType.EMOJI, ActionType.MESSAGE]
    messages = [event for event in store.list_chat_events("momo") if event.event_type == EventType.CHARACTER_MESSAGE]
    assert [event.content for event in messages] == ["等下等下", "🥺", "怎么回事呀？"]
    assert [event.metadata["action_index"] for event in messages] == [0, 1, 2]


class MemoryGateModel(SparseModel):
    def react(self, context):
        self.contexts.append(context)
        self.last_request_messages = []
        self.last_response_text = '{}'
        self.last_attempt = 1
        return PersonReaction(
            actions=[],
            memory_candidates=[
                MemoryCandidate(content="今天喝水了", importance=.2),
                MemoryCandidate(content="用户喜欢红茶", memory_type="USER", importance=.8),
                MemoryCandidate(content="用户下周三第一次做项目汇报", memory_type="USER", importance=.8),
            ],
        )


def test_memory_admission_skips_low_value_and_duplicate(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    now = datetime.now(timezone.utc)
    store.add_memory(Memory(character_id="rin", content="用户喜欢红茶", memory_type="USER", event_time=now - timedelta(days=1), importance=.8, embedding=emb.embed("用户喜欢红茶")))
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, MemoryGateModel(), "persona")

    result = runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=now, content="顺便说一下"))

    assert len(result.created_memory_ids) == 1
    assert [memory.content for memory in store.list_memories("rin")] == ["用户喜欢红茶", "用户下周三第一次做项目汇报"]
    trace = store.get_runtime_trace(result.event.id)
    assert [item["decision"] for item in trace["memory_decisions"]] == ["SKIP_LOW_VALUE", "SKIP_DUPLICATE", "WRITE"]
    assert trace["memory_decisions"][1]["similarity"] == 1.0


def test_reencounter_context_contains_elapsed_relationship_time(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = SparseModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona")
    start = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=start, content="明天要汇报"))
    runtime.handle(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=start + timedelta(days=2), content="结束了"))

    assert "# Relationship Time" in model.contexts[-1]
    assert "距离上次聊天：2 天" in model.contexts[-1]
    assert "不要机械地说‘好久不见’" in model.contexts[-1]


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
