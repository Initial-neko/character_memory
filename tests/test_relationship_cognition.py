from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.character_life_web import attach_character_life_routes
from character_memory.character_relationships import CharacterRelationshipReader
from character_memory.domain.models import Event, EventType, Memory
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def direct_event(store, character, content, *, at=NOW, event_type=EventType.USER_MESSAGE, metadata=None):
    return store.append_event(Event(
        character_id=character, event_type=event_type, event_time=at, content=content,
        metadata=metadata or {"channel": "DIRECT", "conversation_id": f"chat-{character}"},
    ))


def memory(store, character, content, *, source_event_id=None, at=NOW, metadata=None, active=True, superseded_by=None):
    return store.add_memory(Memory(
        character_id=character, content=content, memory_type="USER",
        event_time=at, importance=0.7, source_event_id=source_event_id,
        active=active, superseded_by=superseded_by, metadata=metadata or {},
    ))


def group_event(repo, group, event_id, at, actor_type, actor_id, event_type, content):
    return repo.append_event(GroupEvent(
        conversation_id=group.id, turn_id=f"turn-{event_id}", actor_type=actor_type,
        actor_id=actor_id, event_type=event_type, event_time=at, content=content,
    ))


def group_memory(store, character, content, group, source_event, *, metadata=None):
    return memory(store, character, content, metadata={
        "origin": "GROUP", "conversation_id": group.id,
        "source_conversation_event_id": source_event.id, "turn_id": source_event.turn_id,
        **(metadata or {}),
    })


def test_directional_cognition_is_independent_and_requires_witnessed_peer_message(tmp_path):
    store = SQLiteStore(tmp_path / "directional.db")
    repo = GroupRepository(store)
    group = repo.create_group("shared", ["a", "b"], NOW - timedelta(days=2))
    user_root = group_event(repo, group, "u", NOW - timedelta(hours=3), "USER", "user", "USER_MESSAGE", "你们聊聊星灯")
    from_b = group_event(repo, group, "b", NOW - timedelta(hours=2), "CHARACTER", "b", "CHARACTER_MESSAGE", "星灯已经修好了")
    from_a = group_event(repo, group, "a", NOW - timedelta(hours=1), "CHARACTER", "a", "CHARACTER_MESSAGE", "我会去看看")
    a_memory = group_memory(store, "a", "B 说星灯已经修好了", group, user_root)
    b_memory = group_memory(store, "b", "A 说会去看看", group, user_root)
    repo.add_trace(group.id, user_root.turn_id, "a", user_root.id, NOW - timedelta(hours=1), {
        "recent_group_event_ids": [from_b.id], "created_memory_ids": [a_memory.id],
    })
    repo.add_trace(group.id, user_root.turn_id, "b", user_root.id, NOW - timedelta(hours=1), {
        "recent_group_event_ids": [from_a.id], "created_memory_ids": [b_memory.id],
    })

    # A→B only uses A's memory, verified through the peer message A actually saw.
    a_to_b = CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", offset=0, now=NOW)
    assert [item["memory"]["id"] for item in a_to_b["cognition"]["cognitions"]] == [a_memory.id]
    assert a_to_b["cognition"]["direction"] == "a->b"
    assert a_to_b["cognition"]["cognitions"][0]["status"] == "ACTIVE"
    assert a_to_b["cognition"]["cognitions"][0]["asserted"] is True
    assert any(e["id"] == from_b.id for e in a_to_b["cognition"]["cognitions"][0]["evidence"])

    b_to_a = CharacterRelationshipReader(store).review("b", "a", day="2026-10-14", offset=0, now=NOW)
    assert [item["memory"]["id"] for item in b_to_a["cognition"]["cognitions"]] == [b_memory.id]
    assert b_to_a["cognition"]["direction"] == "b->a"
    assert b_to_a["cognition"]["cognitions"][0]["memory"]["id"] != a_memory.id

    # Correcting a group-derived Memory keeps the replacement attributable to
    # the same witnessed peer input even though the Trace names only the version
    # it originally created.
    replacement = Memory(
        character_id="a", content="B 说星灯已经修好了（更正）", memory_type="EPISODIC",
        event_time=NOW, importance=0.8,
        metadata={**a_memory.metadata, "governance": "CORRECTED",
                  "corrects_memory_id": a_memory.id, "corrected_at": NOW.isoformat()},
    )
    _, replacement = store.supersede_memory(a_memory.id, replacement)
    corrected = CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", offset=0, now=NOW)["cognition"]
    by_id = {item["memory"]["id"]: item for item in corrected["cognitions"]}
    assert by_id[a_memory.id]["status"] == "SUPERSEDED"
    assert by_id[replacement.id]["status"] == "ACTIVE"
    assert any(evidence["id"] == from_b.id for evidence in by_id[replacement.id]["evidence"])

    # Same group, no witnessed B input: a Memory formed from the user's root
    # message must not become A→B cognition just because B is in the room.
    other = repo.create_group("other", ["a", "b"], NOW - timedelta(days=2))
    root2 = group_event(repo, other, "u2", NOW - timedelta(hours=1), "USER", "user", "USER_MESSAGE", "大家好")
    room_only = group_memory(store, "a", "用户打了招呼", other, root2)
    repo.add_trace(other.id, root2.turn_id, "a", root2.id, NOW, {
        "recent_group_event_ids": [], "created_memory_ids": [room_only.id],
    })
    no_peer = CharacterRelationshipReader(store).review("a", "b", day="2026-10-14", offset=0, now=NOW)
    assert room_only.id not in [item["memory"]["id"] for item in no_peer["cognition"]["cognitions"]]
    assert no_peer["cognition"]["counts"]["unattributed"] >= 1
    store.close()


def test_correction_and_forgetting_invalidate_without_deleting_facts(tmp_path):
    store = SQLiteStore(tmp_path / "correction.db")
    source = direct_event(store, "a", "我更喜欢绿茶", metadata={"channel": "DIRECT", "conversation_id": "chat-a"})
    original = memory(store, "a", "用户喜欢红茶", source_event_id=source.id)
    reader = CharacterRelationshipReader(store)
    active = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    assert [item["memory"]["id"] for item in active["cognitions"]] == [original.id]
    assert active["cognitions"][0]["status"] == "ACTIVE"

    replacement = Memory(
        character_id="a", content="用户更喜欢绿茶", memory_type="USER",
        event_time=NOW + timedelta(minutes=1), importance=0.7, source_event_id=source.id,
        metadata={"governance": "CORRECTED", "corrects_memory_id": original.id,
                  "corrected_at": (NOW + timedelta(minutes=1)).astimezone().isoformat()},
    )
    old, replacement = store.supersede_memory(original.id, replacement)
    assert old.id == original.id
    before = store.conn.total_changes
    corrected = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    assert store.conn.total_changes == before
    statuses = {item["memory"]["id"]: item for item in corrected["cognitions"]}
    assert statuses[original.id]["status"] == "SUPERSEDED"
    assert statuses[original.id]["asserted"] is False
    assert statuses[replacement.id]["status"] == "ACTIVE"
    assert any(change["kind"] == "MEMORY_CORRECTED" and change["to_memory_id"] == replacement.id
               for change in corrected["change_history"])

    store.set_memory_active(replacement.id, False)
    forgotten = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    replacement_entry = next(item for item in forgotten["cognitions"] if item["memory"]["id"] == replacement.id)
    assert replacement_entry["status"] == "FORGOTTEN"
    assert replacement_entry["asserted"] is False
    assert forgotten["asserted_count"] == 0

    # Invalidation is a read projection over governance state, never a delete.
    assert store.get_event(source.id) is not None
    assert store.get_memory(original.id) is not None
    assert store.get_memory(replacement.id) is not None
    store.close()


def test_withdrawn_source_and_simulated_scope_do_not_assert(tmp_path):
    store = SQLiteStore(tmp_path / "invalid.db")
    repo = GroupRepository(store)
    group = repo.create_group("shared", ["a", "b"], NOW - timedelta(days=2))
    root = group_event(repo, group, "u", NOW - timedelta(hours=3), "USER", "user", "USER_MESSAGE", "聊聊")
    peer = group_event(repo, group, "b", NOW - timedelta(hours=2), "CHARACTER", "b", "CHARACTER_MESSAGE", "星灯好了")
    linked = group_memory(store, "a", "B 说星灯好了", group, root)
    repo.add_trace(group.id, root.turn_id, "a", root.id, NOW, {
        "recent_group_event_ids": [peer.id], "created_memory_ids": [linked.id],
    })
    reader = CharacterRelationshipReader(store)
    assert reader.review("a", "b", day="2026-10-14", offset=0, now=NOW)["cognition"]["cognitions"][0]["status"] == "ACTIVE"

    repo.update_event_metadata(peer.id, {**peer.metadata, "hidden": True})
    withdrawn = reader.review("a", "b", day="2026-10-14", offset=0, now=NOW)["cognition"]
    entry = withdrawn["cognitions"][0]
    assert entry["status"] == "INVALIDATED"
    assert entry["reason"] == "SOURCE_WITHDRAWN"
    assert entry["asserted"] is False
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_events WHERE id=?", [peer.id]).fetchone()[0] == 1

    # SIMULATED memory metadata is excluded entirely, with only an aggregate count.
    memory(store, "a", "模拟的星灯经历", metadata={
        "experience_scope": {"schema_version": 1, "reality": "SIMULATED"},
    })
    simulated = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    assert simulated["counts"]["simulated_excluded"] >= 1
    assert "模拟的星灯经历" not in str(simulated)

    # A simulated source fact invalidates the derived cognition as well.
    source = direct_event(store, "a", "真实来源", metadata={"channel": "DIRECT", "conversation_id": "chat-a"})
    memory(store, "a", "基于真实来源的认知", source_event_id=source.id)
    store.update_event_metadata(source.id, {
        "channel": "DIRECT", "conversation_id": "chat-a",
        "experience_scope": {"schema_version": 1, "reality": "SIMULATED"},
    })
    after = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    assert "基于真实来源的认知" not in str(after)
    assert after["counts"]["simulated_excluded"] >= 1
    store.close()


def test_bounded_projection_keeps_counts_greater_than_displayed_list(tmp_path):
    store = SQLiteStore(tmp_path / "bounded.db")
    source = direct_event(store, "a", "连续说了很多")
    for index in range(60):
        memory(store, "a", f"关于用户的第 {index} 条认知", source_event_id=source.id,
               at=NOW - timedelta(minutes=index))
    reader = CharacterRelationshipReader(store)
    before = store.conn.total_changes
    cognition = reader.review("a", "user", day="2026-10-14", offset=0, now=NOW)["cognition"]
    assert store.conn.total_changes == before
    assert len(cognition["cognitions"]) == 50
    assert cognition["counts"]["active"] == 60
    assert cognition["asserted_count"] == 60
    assert cognition["counts"]["truncated"] is True
    store.close()


def test_relationship_review_http_exposes_cognition_read_only(tmp_path):
    store = SQLiteStore(tmp_path / "http.db")
    source = direct_event(store, "a", "星灯修好了")
    memory(store, "a", "用户说星灯修好了", source_event_id=source.id)
    app = FastAPI()
    attach_character_life_routes(app, SimpleNamespace(
        read_store=store, ensure_character=lambda _: None,
        web_dir=Path(__file__).parents[1] / "src/character_memory/web"))
    client = TestClient(app)
    before = store.conn.total_changes
    payload = client.get("/v1/characters/a/relationship-review?peer=user&day=2026-10-14&offset=0").json()
    assert store.conn.total_changes == before
    assert payload["read_only"] is True
    cognition = payload["cognition"]
    assert cognition["direction"] == "a->user"
    assert cognition["read_only"] is True
    assert cognition["asserted_count"] == 1
    assert cognition["cognitions"][0]["evidence"][0]["id"] == source.id
    assert any(item["code"] == "DIRECTIONAL" for item in cognition["boundaries"])
    assert isinstance(cognition["open_items"], list)
    store.close()
