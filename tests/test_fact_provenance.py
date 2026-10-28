from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.domain.models import Event, EventType, Memory
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory_web import attach_memory_routes
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


def make_client(store):
    app = FastAPI()
    app.state.character_memory = SimpleNamespace(
        store=lambda: store,
        character_profiles=lambda: [{"id": "momo"}, {"id": "rin"}],
        require_bundle=lambda: SimpleNamespace(embeddings=DeterministicEmbedding()),
    )
    attach_memory_routes(app)
    return TestClient(app)


@pytest.fixture
def store(tmp_path):
    result = SQLiteStore(tmp_path / "provenance.db")
    yield result
    result.close()


def remember(store, *, character_id="momo", source_event_id=None, metadata=None):
    return store.add_memory(Memory(
        character_id=character_id, content="有来源的记忆", event_time=NOW,
        source_event_id=source_event_id, metadata=metadata or {}, embedding=[1.0, 0.0],
    ))


def direct(store, character_id="momo"):
    return store.append_message(Event(character_id=character_id,
        event_type=EventType.USER_MESSAGE, event_time=NOW, content="私聊中的原始事实"))


def group_source(store, *, witnessed=True):
    repo = GroupRepository(store)
    group = repo.create_group("来源验收", ["momo", "rin"], NOW)
    event = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="turn-1",
        actor_type="USER", actor_id="user", event_type="USER_MESSAGE",
        event_time=NOW, content="群聊中不同的原始事实", metadata={"fixture": True}))
    if witnessed:
        repo.add_trace(group.id, event.turn_id, "momo", event.id, NOW, {})
    return repo, group, event


def group_metadata(group, event):
    return {"origin": "GROUP", "conversation_id": group.id,
        "source_conversation_event_id": event.id, "turn_id": event.turn_id}


def payload(store, memory):
    response = make_client(store).get(f"/v1/characters/{memory.character_id}/memories/{memory.id}")
    assert response.status_code == 200, response.text
    return response.json()["memory"]


def provenance(store, memory):
    body = payload(store, memory)
    assert "provenance" in body, "Memory API must expose resolved typed provenance"
    return body["provenance"]


def test_equal_numeric_ids_resolve_distinct_fact_domains_without_writes(store):
    event = direct(store)
    _, group, shared = group_source(store)
    # W3 put Direct and Group facts in one AUTOINCREMENT table, so the two domains
    # no longer share numeric ids. What must hold is that each reference resolves
    # only inside its own domain, which the assertions below still prove.
    assert event.id != shared.id
    one = remember(store, source_event_id=event.id)
    two = remember(store, metadata=group_metadata(group, shared))
    before = store.conn.total_changes
    a, b = provenance(store, one), provenance(store, two)
    assert a["status"] == b["status"] == "RESOLVED"
    assert a["ref"] == {"kind": "EVENT", "id": event.id, "conversation_id": None}
    assert b["ref"] == {"kind": "CONVERSATION_EVENT", "id": shared.id, "conversation_id": group.id}
    assert a["fact"]["content"] == event.content
    assert b["fact"]["content"] == shared.content
    assert b["fact"]["actor_id"] == "user"
    assert b["fact"]["event_time"] == shared.event_time.isoformat()
    assert "content" not in payload(store, two)["source"], "legacy UI is unchanged"
    assert store.conn.total_changes == before


def test_direct_reference_cannot_reveal_another_characters_fact(store):
    other = direct(store, "rin")
    memory = remember(store, source_event_id=other.id)
    body = payload(store, memory)
    assert body["source"] is None
    assert body["provenance"]["status"] == "UNRESOLVED"
    assert body["provenance"]["fact"] is None
    assert other.content not in str(body)


@pytest.mark.parametrize("changes", [
    {"conversation_id": "another-group"}, {"turn_id": "another-turn"},
    {"source_conversation_event_id": 99999},
])
def test_group_mismatch_never_falls_back_to_equal_direct_id(store, changes):
    event = direct(store)
    _, group, shared = group_source(store)
    metadata = {**group_metadata(group, shared), **changes}
    result = provenance(store, remember(store, source_event_id=event.id, metadata=metadata))
    assert result["status"] == "UNRESOLVED"
    assert result["fact"] is None
    assert event.content not in str(result)


@pytest.mark.parametrize("changes", [
    {"source_conversation_event_id": True}, {"source_conversation_event_id": "1"},
    {"source_conversation_event_id": 1.0}, {"conversation_id": " "},
    {"conversation_id": ["wrong"]}, {"turn_id": 1},
])
def test_invalid_legacy_group_reference_is_explicit_and_safe(store, changes):
    _, group, shared = group_source(store)
    result = provenance(store, remember(store, metadata={**group_metadata(group, shared), **changes}))
    assert result["status"] == "INVALID"
    assert result["fact"] is None


def test_group_membership_without_source_witness_does_not_prove_experience(store):
    _, group, shared = group_source(store, witnessed=False)
    result = provenance(store, remember(store, metadata=group_metadata(group, shared)))
    assert result["status"] == "UNRESOLVED"
    assert result["fact"] is None


def test_historical_witness_survives_member_removal_and_archive(store):
    repo, group, shared = group_source(store)
    memory = remember(store, metadata=group_metadata(group, shared))
    repo.replace_members(group.id, ["rin", "new"], NOW)
    repo.archive_group(group.id, NOW)
    result = provenance(store, memory)
    assert result["status"] == "RESOLVED"
    assert result["fact"]["content"] == shared.content


def test_missing_group_tables_are_not_created_by_source_inspection(store):
    memory = remember(store, metadata={"origin": "GROUP", "conversation_id": "absent",
        "source_conversation_event_id": 1})
    before = store.conn.total_changes
    tables = store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert provenance(store, memory)["status"] == "UNRESOLVED"
    assert store.conn.total_changes == before
    assert store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == tables


@pytest.mark.parametrize("column,value", [("metadata_json", "{broken"), ("event_time", "not-time")])
def test_corrupt_shared_fact_cannot_break_memory_api(store, column, value):
    _, group, shared = group_source(store)
    memory = remember(store, metadata=group_metadata(group, shared))
    store.conn.execute(f"UPDATE conversation_events SET {column}=? WHERE id=?", (value, shared.id))
    store.conn.commit()
    assert provenance(store, memory)["status"] == "UNRESOLVED"


def test_group_source_is_preserved_by_correction_and_database_restart(store):
    _, group, shared = group_source(store)
    memory = remember(store, metadata=group_metadata(group, shared))
    client = make_client(store)
    response = client.post(f"/v1/characters/momo/memories/{memory.id}/correct", json={"content": "更正理解但不修改事实"})
    assert response.status_code == 200
    body = response.json()
    assert "provenance" in body["memory"]
    assert body["old_memory"]["provenance"] == body["memory"]["provenance"]
    new_id = body["memory"]["id"]
    db_path = store.conn.execute("PRAGMA database_list").fetchone()[2]
    store.close()
    reopened = SQLiteStore(db_path)
    try:
        new = reopened.get_memory(new_id)
        assert provenance(reopened, new) == body["memory"]["provenance"]
        assert reopened.get_memory(memory.id).superseded_by == new_id
    finally:
        reopened.close()


def test_unrecorded_and_dangling_direct_sources_have_no_invented_fact(store):
    assert provenance(store, remember(store))["status"] == "UNRECORDED"
    result = provenance(store, remember(store, source_event_id=98765))
    assert result["status"] == "UNRESOLVED"
    assert result["fact"] is None


@pytest.mark.parametrize("reference", [
    {"kind": "UNKNOWN", "id": 1}, {"kind": "EVENT", "id": True},
    {"kind": "EVENT", "id": "1"}, {"kind": "EVENT", "id": 1.0},
    {"kind": "EVENT", "id": 0}, {"kind": "EVENT", "id": -1},
    {"kind": "EVENT", "id": 1, "conversation_id": "wrong-domain"},
    {"kind": "CONVERSATION_EVENT", "id": 1},
    {"kind": "CONVERSATION_EVENT", "id": 1, "conversation_id": " "},
    {"kind": "EVENT", "id": 1, "unrecognized": "extra"},
])
def test_fact_ref_rejects_ambiguous_or_invalid_identity(reference):
    from pydantic import ValidationError
    from character_memory.domain.facts import FactRef

    with pytest.raises(ValidationError):
        FactRef.model_validate(reference)


def test_fact_ref_is_immutable_and_round_trips_without_losing_namespace():
    from pydantic import ValidationError
    from character_memory.domain.facts import FactRef

    ref = FactRef(kind="CONVERSATION_EVENT", id=7, conversation_id="group-a")
    assert FactRef.model_validate_json(ref.model_dump_json()) == ref
    assert ref != FactRef(kind="CONVERSATION_EVENT", id=7, conversation_id="group-b")
    with pytest.raises(ValidationError):
        ref.id = 9


def test_all_memory_response_paths_preserve_resolved_provenance(store):
    event = direct(store)
    memory = remember(store, source_event_id=event.id)
    client = make_client(store)
    expected = provenance(store, memory)
    listed = client.get("/v1/characters/momo/memories").json()["memories"]
    assert listed[0]["provenance"] == expected
    for action, value in [("pin", {"pinned": True}), ("active", {"active": False}), ("active", {"active": True})]:
        response = client.post(f"/v1/characters/momo/memories/{memory.id}/{action}", json=value)
        assert response.status_code == 200
        assert response.json()["memory"]["provenance"] == expected
    assert client.get(f"/v1/characters/rin/memories/{memory.id}").status_code == 404


def test_own_group_message_is_a_fact_witness_without_a_trace(store):
    repo, group, _ = group_source(store, witnessed=False)
    event = repo.append_event(GroupEvent(conversation_id=group.id, turn_id="own-turn",
        actor_type="CHARACTER", actor_id="momo", event_type="CHARACTER_MESSAGE",
        event_time=NOW, content="自己确实发出的群消息"))
    result = provenance(store, remember(store, metadata=group_metadata(group, event)))
    assert result["status"] == "RESOLVED"
    assert result["fact"]["actor_id"] == "momo"


@pytest.mark.parametrize("trace_character,trace_turn,trace_conversation", [
    ("rin", "turn-1", None), ("momo", "other-turn", None), ("momo", "turn-1", "other-group"),
])
def test_trace_must_witness_exact_person_conversation_and_turn(store, trace_character, trace_turn, trace_conversation):
    repo, group, event = group_source(store, witnessed=False)
    repo.add_trace(trace_conversation or group.id, trace_turn, trace_character, event.id, NOW, {})
    result = provenance(store, remember(store, metadata=group_metadata(group, event)))
    assert result["status"] == "UNRESOLVED"
    assert result["fact"] is None


def test_group_fields_without_discriminator_do_not_fall_back_to_direct(store):
    event = direct(store)
    result = provenance(store, remember(store, source_event_id=event.id,
        metadata={"source_conversation_event_id": event.id, "conversation_id": "untyped"}))
    assert result["status"] == "INVALID"
    assert result["fact"] is None


@pytest.mark.parametrize("event_id", [2**63, 2**64, 10**100])
def test_oversized_group_id_cannot_overflow_sqlite_or_break_api(store, event_id):
    _, group, shared = group_source(store)
    memory = remember(store, metadata={**group_metadata(group, shared),
        "source_conversation_event_id": event_id})
    assert provenance(store, memory)["status"] == "INVALID"


def test_fact_ref_rejects_values_above_sqlite_integer_domain():
    from pydantic import ValidationError
    from character_memory.domain.facts import FactRef

    assert FactRef(kind="EVENT", id=2**63 - 1).id == 2**63 - 1
    with pytest.raises(ValidationError):
        FactRef(kind="EVENT", id=2**63)
