from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from character_memory.character_life_web import attach_character_life_routes
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.storage.sqlite import SQLiteStore
from character_memory.space_store import SpaceRepository


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def direct(store, character="a", content="昨天修好了星灯", at=NOW, event_type=EventType.USER_MESSAGE, metadata=None):
    return store.append_event(Event(character_id=character, event_type=event_type, event_time=at, content=content, metadata=metadata or {"channel": "DIRECT", "conversation_id": "chat-a"}))


def test_review_combines_recent_real_interaction_and_current_user_plans_without_writes(tmp_path):
    from character_memory.character_relationships import CharacterRelationshipReader
    store = SQLiteStore(tmp_path / "review.db")
    old = direct(store, at=NOW - timedelta(days=1))
    direct(store, content="未来消息", at=NOW + timedelta(days=1))
    direct(store, "b", "另一人物私聊")
    direct(store, content="窗口之前", at=NOW - timedelta(days=8))
    historical = direct(store, content="旧的私聊来源", at=NOW - timedelta(days=21))
    intent = store.add_intent("a", "稍后问用户星灯", "PROACTIVE_MESSAGE", NOW - timedelta(days=20), NOW + timedelta(hours=1), NOW + timedelta(days=1), source_event_id=historical.id)
    done = store.add_intent("a", "执行完了", "PROACTIVE_MESSAGE", NOW, NOW, NOW + timedelta(days=1))
    store.set_intent_status(done, "EXECUTED")
    before = store.conn.total_changes
    data = CharacterRelationshipReader(store).review("a", "user", day="2026-10-14", offset=0, now=NOW)
    assert store.conn.total_changes == before
    assert [item["key"] for item in data["interactions"]] == [f"events:{old.id}"]
    assert [item["source"]["id"] for item in data["pending_intents"]] == [intent]
    assert data["read_only"] and data["pending_scope"] == "CURRENT_USER_DIRECT_PENDING_ALL_CREATION_DATES"
    assert "未来消息" not in str(data) and "另一人物私聊" not in str(data)
    assert CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", now=NOW)["pending_intents"] == []
    midnight = direct(store, content="所选日后一天午夜", at=datetime(2026, 10, 14, tzinfo=timezone.utc))
    prior_day = CharacterRelationshipReader(store).review("a", "user", day="2026-10-13", offset=0, now=NOW)
    assert f"events:{midnight.id}" not in [item["key"] for item in prior_day["interactions"]]
    store.close()


def test_space_counterpart_and_unknown_legacy_plan_provenance(tmp_path):
    from character_memory.character_relationships import CharacterRelationshipReader
    store = SQLiteStore(tmp_path / "space-review.db")
    repo = SpaceRepository(store)
    post = repo.create_post("b", "公开讨论", NOW - timedelta(days=1))
    to_owner = repo.add_comment(post.id, "a", "回应动态主人", NOW - timedelta(hours=4))
    parent = repo.add_comment(post.id, "c", "另一位实际评论者", NOW - timedelta(hours=3))
    reply = repo.add_comment(post.id, "a", "回应实际评论者", NOW - timedelta(hours=2), reply_to_comment_id=parent.id)
    missing = repo.add_comment(post.id, "a", "对象缺失不猜测", NOW - timedelta(hours=1))
    store.conn.execute("UPDATE space_comments SET reply_to_comment_id=99999 WHERE id=?", [missing.id])
    reader = CharacterRelationshipReader(store)
    before = store.conn.total_changes
    assert [i["key"] for i in reader.review("a", "b", day="2026-10-14", offset=0, now=NOW)["interactions"]] == [f"comment:{to_owner.id}"]
    assert [i["key"] for i in reader.review("a", "c", day="2026-10-14", offset=0, now=NOW)["interactions"]] == [f"comment:{reply.id}"]
    assert store.conn.total_changes == before
    legacy = store.add_intent("a", "来源未知的旧意图", "PROACTIVE_MESSAGE", NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1))
    bad = store.add_intent("a", "坏时间不推测", "PROACTIVE_MESSAGE", NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1))
    store.conn.execute("UPDATE intents SET earliest_at='malformed' WHERE id=?", [bad])
    plans = reader.review("a", "user", day="2026-10-14", now=NOW)["pending_intents"]
    assert [i["source"]["id"] for i in plans] == [legacy]
    assert plans[0]["source_known"] is False
    store.close()


def test_group_review_requires_actual_scoped_participation_and_join_boundary(tmp_path):
    from character_memory.character_relationships import CharacterRelationshipReader
    store = SQLiteStore(tmp_path / "group-review.db")
    repo = GroupRepository(store)
    group = repo.create_group("shared", ["a", "b", "c"], NOW - timedelta(days=3))
    def message(conversation, at, actor="b", text="明天要不要一起采星草？"):
        return repo.append_event(GroupEvent(conversation_id=conversation, turn_id="one", actor_type="CHARACTER", actor_id=actor, event_type="CHARACTER_MESSAGE", event_time=at, content=text))
    actual = message(group.id, NOW - timedelta(days=1))
    repo.add_trace(group.id, "one", "a", actual.id, NOW - timedelta(hours=23), {"actions": []})
    message(group.id, NOW - timedelta(hours=1), text="未被人物处理")
    future = message(group.id, NOW + timedelta(days=1), text="未来事件")
    repo.add_trace(group.id, "one", "a", future.id, NOW, {})
    other = repo.create_group("other", ["b", "c"], NOW - timedelta(days=3))
    foreign = message(other.id, NOW - timedelta(days=1), text="跨群数据")
    repo.add_trace(group.id, "one", "a", foreign.id, NOW, {})
    data = CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", offset=0, now=NOW)
    assert [item["key"] for item in data["interactions"]] == [f"group:{actual.id}"]
    assert data["pending_intents"] == []
    assert data["interactions"][0]["content"] == "明天要不要一起采星草？"
    store.conn.execute("UPDATE conversation_members SET joined_at_epoch=? WHERE conversation_id=? AND actor_id='a'", [int((NOW - timedelta(hours=12)).timestamp() * 1_000_000), group.id])
    assert CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", now=NOW)["interactions"] == []
    store.close()


def test_pending_plan_source_scope_limits_and_route_validation(tmp_path):
    from character_memory.character_relationships import CharacterRelationshipReader
    store = SQLiteStore(tmp_path / "bounds.db")
    good = direct(store)
    social = direct(store, content="不能派发的群来源", metadata={"channel": "GROUP"})
    for index in range(20):
        direct(store, content=f"消息 {index} " + "x" * 1000, at=NOW - timedelta(minutes=index + 1))
        store.add_intent("a", f"计划 {index}", "PROACTIVE_MESSAGE", NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1), source_event_id=good.id)
    store.add_intent("a", "群邀请不是私聊计划", "PROACTIVE_MESSAGE", NOW, NOW, NOW + timedelta(days=1), source_event_id=social.id)
    expired = store.add_intent("a", "已经过期", "PROACTIVE_MESSAGE", NOW, NOW - timedelta(days=2), NOW - timedelta(days=1), source_event_id=good.id)
    data = CharacterRelationshipReader(store).review("a", "user", day="2026-10-14", now=NOW)
    assert len(data["interactions"]) == 10 and data["interactions_has_more"]
    assert len(data["pending_intents"]) == 5 and data["pending_has_more"]
    assert all(len(item["content"]) <= 500 for item in data["interactions"])
    assert expired not in [item["source"]["id"] for item in data["pending_intents"]]
    assert "群邀请不是私聊计划" not in str(data)
    app = FastAPI()
    def ensure(character):
        if character not in {"a", "b"}:
            raise HTTPException(404, "unknown")
    attach_character_life_routes(app, SimpleNamespace(read_store=store, ensure_character=ensure, web_dir=Path(__file__).parents[1] / "src/character_memory/web"))
    client = TestClient(app)
    assert client.get("/v1/characters/a/relationship-review?peer=user").json()["read_only"]
    assert client.get("/v1/characters/missing/relationship-review?peer=user").status_code == 404
    assert client.get("/v1/characters/a/relationship-review?peer=missing").status_code == 404
    assert client.get("/v1/characters/a/relationship-review?peer=a").status_code == 422
    assert client.get("/v1/characters/a/relationship-review?peer=user&day=bad").status_code == 422
    assert client.post("/v1/characters/a/relationship-review?peer=user").status_code == 405
    store.close()


def test_real_group_turns_retain_peer_messages_actually_seen_by_follower(tmp_path):
    from character_memory.application.clock import FixedClock
    from character_memory.application.group_conversation_service import GroupConversationService
    from character_memory.character_relationships import CharacterRelationshipReader
    from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
    from character_memory.memory.embedding import DeterministicEmbedding
    from test_p0_11_group_conversation import SequenceModel, runtime_for

    store = SQLiteStore(tmp_path / "real-turns.db")
    clock = FixedClock(NOW - timedelta(hours=2))
    model = SequenceModel([
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="我刚修好了星灯。")]),
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="B，你修好的星灯真漂亮。")]),
        PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="还想聊聊 B 刚修好的星灯。")]),
        PersonReaction(actions=[]),
    ])
    embeddings = DeterministicEmbedding()
    service = GroupConversationService(store, {
        name: runtime_for(store, embeddings, model, name) for name in ("a", "b")
    }, clock, profiles=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
    group = service.create_group("real shared turns", ["b", "a"])
    clock.set(NOW - timedelta(hours=1))
    first = service.send(group.id, "说说星灯")
    peer_event = next(event for event in service.repo.list_turn_events(group.id, first["turn_id"]) if event.actor_id == "b")
    assert "B: 我刚修好了星灯。" in model.contexts[1]
    clock.set(NOW - timedelta(minutes=30))
    second = service.send(group.id, "再聊聊 B 的星灯")
    assert second["speaker_order"] == ["a", "b"]
    assert "B: 我刚修好了星灯。" in model.contexts[2]
    # Real traces root at the User fact, not at B's reply.
    traces = service.repo.list_turn_traces(group.id, first["turn_id"])
    assert all(trace["source_conversation_event_id"] != peer_event.id for trace in traces)
    review = CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", offset=0, now=NOW)
    if not any("recent_group_event_ids" in trace for trace in traces):
        # This stack is based on #283; #282 supplies real input receipts.
        assert review["interactions"] == []
        store.close()
        pytest.skip("full real Group provenance requires #282 + #283 + #286 integration")
    assert [item["key"] for item in review["interactions"]] == [f"group:{peer_event.id}"]
    store.close()


def test_trace_input_evidence_types_group_scope_and_historical_provenance(tmp_path):
    from character_memory.character_relationships import CharacterRelationshipReader
    store = SQLiteStore(tmp_path / "trace-evidence.db")
    repo = GroupRepository(store)
    group = repo.create_group("shared", ["a", "b"], NOW - timedelta(days=1))
    root = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="one", actor_type="USER", actor_id="user", event_type="USER_MESSAGE", event_time=NOW - timedelta(hours=2), content="说说星灯"))
    peer = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="one", actor_type="CHARACTER", actor_id="b", event_type="CHARACTER_MESSAGE", event_time=root.event_time, content="星灯已经修好"))
    reader = CharacterRelationshipReader(store)
    def record(trace):
        store.conn.execute("DELETE FROM conversation_runtime_traces")
        repo.add_trace(group.id, "one", "a", root.id, NOW - timedelta(hours=1), trace)
        return reader.review("a", "b", day="2026-10-14", offset=0, now=NOW)["interactions"]
    assert record({"recent_group_event_ids": [peer.id]})[0]["key"] == f"group:{peer.id}"
    assert record({"recent_group_event_ids": [str(peer.id), float(peer.id)]}) == []
    assert record({"recent_group_event_ids": {"0": peer.id}}) == []
    assert record({"recent_group_event_ids": [0] * 14 + [peer.id]}) == []
    history = {"conversation_id": group.id, "source_conversation_event_id": peer.id}
    assert record({"shared_history_retrieval": {"events": [history]}})[0]["key"] == f"group:{peer.id}"
    assert record({"shared_history_retrieval": {"events": [{**history, "conversation_id": "other"}]}}) == []
    assert record({"shared_history_retrieval": {"events": [str(peer.id), peer.id, "bad json"]}}) == []
    record({})
    store.conn.execute("UPDATE conversation_runtime_traces SET trace_json='malformed'")
    assert reader.review("a", "b", day="2026-10-14", offset=0, now=NOW)["interactions"] == []
    store.close()
