from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from character_memory.application.async_conversation import ConversationEventHub, ReactionScheduler
from character_memory.application.chat_service import build_user_event
from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService, SupersededGroupReaction
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class _SequenceModel(PersonModel):
    def __init__(self, messages):
        self.messages = list(messages)

    def react(self, context):
        message = self.messages.pop(0)
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message=message)])

    def react_for_session(self, context, session_id):
        return self.react(context)

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _runtime(store, embeddings, model, persona):
    return PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, persona)


def test_direct_watermark_uses_persisted_arrival_order_not_event_time(tmp_path):
    store = SQLiteStore(tmp_path / "direct-order.db")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    first = store.append_event(build_user_event("先到", character_id="rin", conversation_id="conv", at=now))
    second = store.append_event(build_user_event("后到但时间更早", character_id="rin", conversation_id="conv", at=now - timedelta(hours=2)))
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: None, lambda: [], hub)

    assert second.id > first.id
    assert scheduler._latest_direct_user_id(store, "rin", "conv") == second.id

    scheduler.close()
    hub.close()
    store.close()


def test_group_watermark_uses_persisted_arrival_order_not_event_time(tmp_path):
    store = SQLiteStore(tmp_path / "group-order.db")
    repo = GroupRepository(store)
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    group = repo.create_group("顺序群", ["rin", "momo"], now)
    first = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="t1", actor_type="USER", actor_id="user", event_type="USER_MESSAGE", event_time=now, content="先到"))
    second = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="t2", actor_type="USER", actor_id="user", event_type="USER_MESSAGE", event_time=now - timedelta(hours=2), content="后到但时间更早"))
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: None, lambda: [], hub)

    assert second.id > first.id
    assert scheduler._latest_group_user_id(SimpleNamespace(store=store), group.id) == second.id

    scheduler.close()
    hub.close()
    store.close()


def test_group_supersession_after_first_member_keeps_committed_fact_and_stops_later_member(tmp_path):
    store = SQLiteStore(tmp_path / "group-partial.db")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    embeddings = DeterministicEmbedding()
    model = _SequenceModel(["Rin已经发出", "Momo不该发出"])
    service = GroupConversationService(
        store,
        {
            "rin": _runtime(store, embeddings, model, "Rin"),
            "momo": _runtime(store, embeddings, model, "Momo"),
        },
        FixedClock(now),
        profiles=[{"id": "rin", "name": "Rin"}, {"id": "momo", "name": "Momo"}],
    )
    group = service.create_group("打断群", ["rin", "momo"])
    source = service.persist_user_event(group.id, "第一条")
    latest_id = source.id

    def guard():
        return latest_id == source.id

    def on_member(decision):
        nonlocal latest_id
        assert decision["character_id"] == "rin"
        newer = service.persist_user_event(group.id, "在第二个人判断前补充")
        latest_id = newer.id

    with pytest.raises(SupersededGroupReaction):
        service.react_from_event(source, commit_guard=guard, on_member=on_member)

    events = service.repo.list_events(group.id)
    assert [(event.actor_type, event.actor_id, event.content) for event in events] == [
        ("USER", "user", "第一条"),
        ("CHARACTER", "rin", "Rin已经发出"),
        ("USER", "user", "在第二个人判断前补充"),
    ]
    assert not any(event.actor_id == "momo" for event in events)
    first_turn_traces = service.repo.list_turn_traces(group.id, source.turn_id)
    assert [trace["character_id"] for trace in first_turn_traces] == ["rin"]
    store.close()
