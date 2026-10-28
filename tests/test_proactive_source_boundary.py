from datetime import datetime, timedelta, timezone

import pytest

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.application.proactive_service import ProactiveService
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class ReplyModel(PersonModel):
    def __init__(self):
        self.calls = []

    def react(self, context):
        self.calls.append(context)
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="今天汇报结束了吗？")])


@pytest.mark.parametrize("event_type,metadata", [
    (EventType.SPACE_COMMENT_RECEIVED, {"channel": "SPACE", "commenter_id": "rin"}),
    (EventType.SPACE_POST_SEEN, {}),
    (EventType.WORLD_OBSERVATION, {}),
    (EventType.USER_MESSAGE, {"channel": "GROUP"}),
    (EventType.TIME_TICK, {"source_conversation_event_id": 9}),
])
def test_legacy_social_intent_never_becomes_direct_message(tmp_path, event_type, metadata):
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    path = tmp_path / "legacy.db"
    store = SQLiteStore(path)
    # A row written before the W3 migration: the preserved legacy log is exactly
    # the shape this test must keep refusing as a Direct source.
    source = store.append_legacy_event(Event(character_id="momo", event_type=event_type,
        event_time=now - timedelta(hours=2), content="和铃继续聊植物", metadata=metadata))
    intent_id = store.add_intent("momo", "明天继续回复铃", "PROACTIVE_MESSAGE",
        now - timedelta(hours=2), now - timedelta(hours=1), now + timedelta(hours=4), source_event_id=source.id)
    store.close()
    # Existing durable rows survive an upgrade/restart; admission-time checks
    # alone cannot prevent the old intent from being dispatched.
    store = SQLiteStore(path)
    model = ReplyModel()
    embeddings = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "momo")
    service = ProactiveService(store, ChatService(store, {"momo": runtime}, FixedClock(now)))
    try:
        assert service.has_due(["momo"], now) is False
        assert service.dispatch_due(["momo"], now) == []
        assert model.calls == []
        assert store.list_events("momo", event_type=EventType.CHARACTER_MESSAGE.value) == []
        row = next(row for row in store.list_intents("momo") if row["id"] == intent_id)
        assert row["status"] == "SUPPRESSED"
        assert store.proactive_dispatch_state("momo") is None
        assert store.get_event(source.id).content == "和铃继续聊植物"
        audit = store.conn.execute("SELECT decision FROM intent_deferral_audit WHERE intent_id=?", (intent_id,)).fetchall()
        assert [row["decision"] for row in audit] == ["NON_DIRECT_SOURCE"]
    finally:
        store.close()


def test_direct_wake_context_keeps_social_origin_and_user_recipient(tmp_path):
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "context.db")
    store.append_event(Event(character_id="momo", event_type=EventType.SPACE_COMMENT_RECEIVED,
        event_time=now - timedelta(minutes=1), content="铃回复：明天继续聊植物",
        metadata={"channel": "SPACE", "commenter_id": "rin", "conversation_id": "space:12:thread:4"}))
    model = ReplyModel()
    embeddings = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "momo")
    chat = ChatService(store, {"momo": runtime}, FixedClock(now))
    try:
        result = chat.dispatch_wake(character_id="momo", at=now, conversation_id="my-chat")
        context = model.calls[0]
        assert result.event.metadata["channel"] == "DIRECT"
        assert "当前接收对象是与你私聊的用户" in context
        assert '"channel": "SPACE"' in context
        assert '"commenter_id": "rin"' in context
        assert '"conversation_id": "space:12:thread:4"' in context
        assert "铃回复：明天继续聊植物" in context
    finally:
        store.close()


def test_group_context_does_not_claim_the_user_is_direct_recipient():
    from character_memory.runtime.context import compile_context

    event = Event(character_id="momo", event_type=EventType.TIME_TICK,
        event_time=datetime(2026, 10, 13, tzinfo=timezone.utc), content="群聊机会",
        metadata={"channel": "GROUP", "source_conversation_event_id": 12})
    context = compile_context("momo", "", [], event)
    assert "当前表达渠道：GROUP" in context
    assert "当前接收对象是与你私聊的用户" not in context


def test_dispatch_without_preflight_skips_social_and_preserves_direct_intent(tmp_path):
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "mixed.db")
    source = store.append_event(Event(character_id="momo", event_type=EventType.SPACE_COMMENT_RECEIVED,
        event_time=now - timedelta(hours=2), content="回复另一角色"))
    social = store.add_intent("momo", "回复铃", "MESSAGE", now - timedelta(hours=2),
        now - timedelta(hours=1), now + timedelta(hours=4), source_event_id=source.id)
    user = store.append_message(Event(character_id="momo", event_type=EventType.USER_MESSAGE,
        event_time=now - timedelta(minutes=30), content="晚点问问我", metadata={"conversation_id": "my-chat"}))
    direct = store.add_intent("momo", "问问用户", "MESSAGE", now - timedelta(minutes=30),
        now - timedelta(minutes=10), now + timedelta(hours=4), source_event_id=user.id)
    model = ReplyModel()
    embeddings = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, "momo")
    service = ProactiveService(store, ChatService(store, {"momo": runtime}, FixedClock(now)))
    try:
        outcomes = service.dispatch_due(["momo"], now)
        assert [row["intent_id"] for row in outcomes] == [direct]
        assert len(model.calls) == 1
        assert next(row for row in store.list_intents("momo") if row["id"] == social)["status"] == "SUPPRESSED"
        message = store.list_events("momo", event_type=EventType.CHARACTER_MESSAGE.value)[0]
        assert message.metadata["conversation_id"] == "my-chat"
    finally:
        store.close()
