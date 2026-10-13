from datetime import datetime, timedelta, timezone

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import Event, EventType, PersonReaction
from character_memory.group_store import GroupEvent
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.storage.sqlite import SQLiteStore
from test_p0_11_group_conversation import SequenceModel, runtime_for


def test_old_shared_fact_reaches_model_and_trace_after_restart(tmp_path):
    path = tmp_path / "shared.db"
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    store = SQLiteStore(path)
    embeddings = DeterministicEmbedding()
    model = SequenceModel([PersonReaction(actions=[]), PersonReaction(actions=[])])
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, model, name) for name in ("rin", "momo")
    }, FixedClock(at))
    group = service.create_group("shared", ["rin", "momo"])
    old = service.persist_user_event(group.id, "我们刚一起修好了星灯。", at=at)
    invite = service.persist_user_event(group.id, "明天要不要一起采星草？", at=at)
    # Beyond even the public history API's 500-row cap, not merely a larger recent window.
    for index in range(550):
        service.persist_user_event(group.id, f"无关闲聊 {index}", at=at + timedelta(minutes=index + 1))
    store.close()
    store = SQLiteStore(path)
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, model, name) for name in ("rin", "momo")
    }, FixedClock(at + timedelta(days=1)))
    result = service.send(group.id, "还记得星灯和星草吗？")
    assert len(model.contexts) == 2
    assert all("我们刚一起修好了星灯。" in context for context in model.contexts)
    assert all("明天要不要一起采星草？" in context for context in model.contexts)
    assert all("邀请不等于已接受或已完成" in context for context in model.contexts)
    traces = service.repo.list_turn_traces(group.id, result["turn_id"])
    for trace in traces:
        evidence = trace["shared_history_retrieval"]
        assert {item["source_conversation_event_id"] for item in evidence["events"]} == {old.id, invite.id}
        assert evidence["context_chars"] <= evidence["char_budget"]
        assert len(trace["recent_group_event_ids"]) <= 14
    assert store.list_memories("rin") == []
    store.close()


def test_real_ordered_group_trace_records_input_ids_not_later_own_output(tmp_path):
    from character_memory.domain.models import ActionDecision, ActionType
    store = SQLiteStore(tmp_path / "seen-inputs.db")
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    model = SequenceModel([
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="我刚修好了星灯。")]),
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="我看到了你修好的星灯。")]),
    ])
    embeddings = DeterministicEmbedding()
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, model, name) for name in ("b", "a")
    }, FixedClock(at), profiles=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
    group = service.create_group("ordered", ["b", "a"])
    result = service.send(group.id, "说说星灯")
    events = service.repo.list_turn_events(group.id, result["turn_id"])
    user, b, a = events
    traces = {trace["character_id"]: trace for trace in service.repo.list_turn_traces(group.id, result["turn_id"])}
    assert "B: 我刚修好了星灯。" in model.contexts[1]
    assert traces["b"]["recent_group_event_ids"] == [user.id]
    assert traces["a"]["recent_group_event_ids"] == [user.id, b.id]
    assert a.id not in traces["a"]["recent_group_event_ids"]
    store.close()


def test_history_recall_scope_membership_watermark_and_budget(tmp_path):
    store = SQLiteStore(tmp_path / "scope.db")
    embeddings = DeterministicEmbedding()
    model = SequenceModel([])
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, model, name) for name in ("rin", "momo", "new")
    }, FixedClock(at))
    group = service.create_group("one", ["rin", "momo"])
    other = service.create_group("other", ["rin", "momo"])
    old = service.persist_user_event(group.id, "星灯真实共享 " + "x" * 5000, at=at)
    service.persist_user_event(other.id, "星灯跨群秘密", at=at)
    store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=at, content="星灯私人秘密"))
    service.repo.append_event(GroupEvent(conversation_id=group.id, turn_id="hidden", actor_type="SYSTEM", actor_id="system", event_type="GROUP_OPPORTUNITY", event_time=at, content="星灯隐藏机会", metadata={"hidden": True}))
    source = service.persist_user_event(group.id, "星灯", at=at + timedelta(days=1))
    service.persist_user_event(group.id, "星灯未来时间", at=at + timedelta(days=2))
    service.persist_user_event(group.id, "星灯迟到回填", at=at)
    rows = service.repo.recall_shared_events(group.id, "rin", "星灯", at=source.event_time, max_id=source.id, exclude_ids=[source.id])
    assert [event.id for event in rows] == [old.id]
    assert service.repo.recall_shared_events(other.id, "new", "星灯", at=source.event_time, max_id=source.id) == []
    service.repo.replace_members(group.id, ["rin", "momo", "new"], source.event_time)
    assert service.repo.recall_shared_events(group.id, "new", "星灯", at=source.event_time, max_id=source.id, exclude_ids=[source.id]) == []
    # Retained members keep the join boundary instead of losing their shared past.
    assert service.repo.recall_shared_events(group.id, "rin", "星灯", at=source.event_time, max_id=source.id, exclude_ids=[source.id])[0].id == old.id
    assert service.repo.recall_shared_events(group.id, "rin", "", at=source.event_time, max_id=source.id) == []
    context, evidence = service._shared_history_context(group.id, "rin", "星灯", source, service._recent_as_events(group.id, source_event=source, character_id="rin"))
    # Source and same-turn completed replies may be read; later backfills and future facts may not.
    visible = service._recent_as_events(group.id, source_event=source, character_id="rin")
    assert all("未来时间" not in event.content and "迟到回填" not in event.content for event in visible)
    assert "私人秘密" not in context and "跨群秘密" not in context
    store.close()


def test_history_projection_caps_rows_text_and_keeps_completed_turn_replies(tmp_path):
    store = SQLiteStore(tmp_path / "budget.db")
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    embeddings = DeterministicEmbedding()
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, SequenceModel([]), name) for name in ("rin", "momo")
    }, FixedClock(at))
    group = service.create_group("budget", ["rin", "momo"])
    for index in range(10):
        service.persist_user_event(group.id, f"星灯 {index} " + "x" * 5000, at=at)
    source = service.persist_user_event(group.id, "星灯", at=at + timedelta(days=1))
    context, evidence = service._shared_history_context(group.id, "rin", "星灯", source, [])
    assert len(evidence["events"]) == 4
    assert len(context) <= 2400
    assert all(len(row["content"]) <= 500 for row in evidence["events"])
    reply = service.repo.append_event(GroupEvent(conversation_id=group.id, turn_id=source.turn_id, actor_type="CHARACTER", actor_id="momo", event_type="CHARACTER_MESSAGE", event_time=source.event_time, content="当轮已提交回应"))
    next_user = service.persist_user_event(group.id, "下一轮星灯", at=source.event_time)
    visible = service._recent_as_events(group.id, source_event=source, character_id="rin")
    ids = {event.metadata["group_event_id"] for event in visible}
    assert reply.id in ids and next_user.id not in ids
    store.close()
