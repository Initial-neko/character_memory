"""W3: Direct conversation facts have one writer, one reader and no dual read.

These tests pin the contract the migration ADR froze: chat messages never enter
the legacy ``events`` table again, Direct ids stay above the shared floor, a
legacy id resolves through its alias, and Group queries cannot count or search a
Direct row.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

import pytest

from character_memory.domain.facts import FactRef
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.storage.conversation_facts import (
    CONVERSATION_FACT_ID_FLOOR,
    ConversationFactReader,
    LegacyDirectWriteClosed,
    canonical_direct_conversation_id,
)
from character_memory.storage.message_search import MessageSearchRepository
from character_memory.storage.message_receipts import MessageReceiptRepository, ReceiptConflict
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    result = SQLiteStore(tmp_path / "facts.db")
    yield result
    result.close()


def user(store, text="你好", *, character="rin", raw="conv-a", at=NOW, metadata=None):
    return store.fact_writer.append_direct(
        Event(
            character_id=character,
            event_type=EventType.USER_MESSAGE,
            event_time=at,
            content=text,
            metadata={"conversation_id": raw, **(metadata or {})},
        ),
        owner_character_id=character,
        raw_conversation_id=raw,
    )


def reply(store, source, text="晚上好", *, character="rin", raw="conv-a", at=NOW):
    return store.fact_writer.append_direct(
        Event(
            character_id=character,
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=at,
            content=text,
            metadata={"conversation_id": raw, "source_event_id": source.id},
        ),
        owner_character_id=character,
        raw_conversation_id=raw,
    )


def group(repo, *, character="momo", text="群聊", at=NOW):
    from datetime import timedelta

    group_ = repo.create_group("测试群", ["rin", "momo"], at)
    return group_, repo.append_event(
        GroupEvent(
            conversation_id=group_.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id=character,
            event_type="CHARACTER_MESSAGE",
            event_time=at,
            content=text,
        )
    )


def test_direct_write_never_enters_the_legacy_event_log(store):
    saved = user(store)
    assert saved.id >= CONVERSATION_FACT_ID_FLOOR
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')"
    ).fetchone()[0]
    assert rows == 0


def test_direct_append_event_is_closed_not_silently_legacy(store):
    with pytest.raises(LegacyDirectWriteClosed):
        store.append_event(
            Event(
                character_id="rin",
                event_type=EventType.USER_MESSAGE,
                event_time=NOW,
                content="bypass",
                metadata={"conversation_id": "conv-a"},
            )
        )
    # Non-conversation event types keep the legacy primitive.
    action = store.append_event(
        Event(character_id="rin", event_type=EventType.ACTION, event_time=NOW, content="act")
    )
    assert action.id < CONVERSATION_FACT_ID_FLOOR


def test_conversation_identity_is_deterministic_and_per_character(store):
    first = user(store, character="rin", raw="same")
    other = user(store, character="momo", raw="same")
    ids = {
        store.conn.execute(
            "SELECT conversation_id FROM conversation_events WHERE id=?", (event.id,)
        ).fetchone()[0]
        for event in (first, other)
    }
    assert len(ids) == 2
    assert canonical_direct_conversation_id("rin", "same") in ids
    # Missing raw id collapses into the documented per-character archive.
    scoped = user(store, character="rin", raw="", metadata={"conversation_id": ""})
    conversation = store.conn.execute(
        "SELECT conversation_id FROM conversation_events WHERE id=?", (scoped.id,)
    ).fetchone()[0]
    assert conversation.endswith("legacy-unscoped")


def test_history_page_and_turns_read_the_unified_domain(store):
    source = user(store)
    answer = reply(store, source)
    page = store.fact_reader.list_page("rin")
    assert [event.id for event in page.events] == [source.id, answer.id]
    turns = {
        row[0]
        for row in store.conn.execute(
            "SELECT turn_id FROM conversation_events WHERE id IN (?,?)", (source.id, answer.id)
        )
    }
    assert f"direct-turn-ce-{source.id}" in turns
    assert f"direct-turn-ce-{source.id}" in turns  # reply shares the user turn


def test_list_chat_events_and_count_exclude_group_rows(store):
    source = user(store)
    repo = GroupRepository(store)
    _, group_event = group(repo)
    assert [event.id for event in store.list_chat_events("rin")] == [source.id]
    assert store.count_character_messages("momo") == 0
    assert repo.count_character_messages("momo") == 1
    assert repo.count_character_messages("rin") == 0


def test_group_activity_ignores_direct_only_facts(store):
    repo = GroupRepository(store)
    # rin speaks only in Direct chats; momo has one shared group fact.
    during = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    user(store, character="rin", at=during)
    reply(store, user(store, character="rin", at=during), character="rin", at=during)
    group(repo, character="momo", at=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc))
    cutoff = int(during.timestamp() * 1_000_000)
    assert repo.has_character_activity_since("rin", cutoff) is False
    assert repo.has_character_activity_since("momo", cutoff) is True


def test_direct_search_never_reports_group_scope(store):
    repo = GroupRepository(store)
    group_conversation, _ = group(repo, text="共同暗号")
    user(store, text="共同暗号")
    search = MessageSearchRepository(store)
    direct = search.search_direct("共同暗号")
    grouped = search.search_group("共同暗号")
    assert [item["scope"] for item in direct] == ["DIRECT"]
    assert [item["scope"] for item in grouped] == ["GROUP"]
    assert direct[0]["event_id"] != grouped[0]["event_id"]
    assert direct[0]["character_id"] == "rin"


def test_aliased_legacy_id_resolves_and_jumps(store):
    # Simulate the migration's output: a preserved legacy row plus its alias.
    store.append_legacy_event(
        Event(
            character_id="rin",
            event_type=EventType.USER_MESSAGE,
            event_time=NOW,
            content="迁移前消息",
            metadata={"conversation_id": "conv-a"},
        )
    )
    legacy = store.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    canonical = user(store, text="迁移前消息", at=NOW)
    store.conn.execute(
        "INSERT INTO conversation_fact_aliases(legacy_kind,legacy_id,character_id,legacy_conversation_id,"
        "conversation_id,conversation_event_id,content_sha256,metadata_sha256,status,reason,migrated_at) "
        "VALUES('EVENT',?,?,?,?,?,'x','y','MIGRATED','',?)",
        (legacy["id"], "rin", "conv-a", canonical_direct_conversation_id("rin", "conv-a"),
         canonical.id, NOW.isoformat()),
    )
    store.conn.commit()
    resolved = store.get_event(legacy["id"])
    assert resolved is not None and resolved.id == canonical.id
    reader = ConversationFactReader(store)
    assert reader.canonical_id_for_legacy(legacy["id"]) == canonical.id
    assert reader.resolve_direct(FactRef(kind="EVENT", id=legacy["id"]), character_id="rin").id == canonical.id


def test_receipt_rejects_group_ref_for_a_direct_conversation(store):
    repo = GroupRepository(store)
    _, group_event = group(repo)
    receipts = MessageReceiptRepository(store)
    direct_conversation = user(store)
    conversation_id = store.conn.execute(
        "SELECT conversation_id FROM conversation_events WHERE id=?", (direct_conversation.id,)
    ).fetchone()[0]

    # A Direct conversation id paired with a Group event id must not validate on
    # replay, even though id+conversation_id alone would both exist.
    first = receipts.accept(
        ["DIRECT", "rin", conversation_id],
        "req-group-id",
        {"message": "x"},
        lambda: {"event_id": group_event.id},
        source_kind="CONVERSATION_EVENT",
        conversation_id=conversation_id,
    )
    assert first.created is True
    with pytest.raises(ReceiptConflict):
        receipts.accept(
            ["DIRECT", "rin", conversation_id],
            "req-group-id",
            {"message": "x"},
            lambda: {"event_id": group_event.id},
            source_kind="CONVERSATION_EVENT",
            conversation_id=conversation_id,
        )


def test_metadata_update_refuses_a_same_numbered_stranger(store):
    mine = user(store, character="rin", raw="mine")
    theirs = user(store, character="momo", raw="theirs")
    assert store.fact_writer.update_direct_metadata(
        theirs.id, {"voice_status": "ready"}, character_id="rin", raw_conversation_id="mine"
    ) is None
    updated = store.fact_writer.update_direct_metadata(
        mine.id, {"voice_status": "ready"}, character_id="rin", raw_conversation_id="mine"
    )
    assert updated is not None and updated.metadata["voice_status"] == "ready"


def test_sequence_reservation_keeps_new_ids_above_the_floor(store):
    repo = GroupRepository(store)
    group_conversation, group_event = group(repo)
    assert group_event.id >= CONVERSATION_FACT_ID_FLOOR
    user(store)
    # A brand-new database file also reserves on first open.
    other = SQLiteStore(Path(store.path).parent / "second.db")
    try:
        second_group = GroupRepository(other)
        _, event = group(second_group)
        assert event.id >= CONVERSATION_FACT_ID_FLOOR
    finally:
        other.close()


def test_get_event_is_domain_aware(store):
    source = user(store)
    assert store.get_event(source.id).event_type == EventType.USER_MESSAGE
    repo = GroupRepository(store)
    _, group_event = group(repo)
    # Group rows use a separate FactRef kind and must not resolve here.
    assert store.get_event(group_event.id) is None
