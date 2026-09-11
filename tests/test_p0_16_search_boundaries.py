from __future__ import annotations

from datetime import datetime, timedelta, timezone

from character_memory.domain.models import Memory
from character_memory.group_store import GroupRepository
from character_memory.application.group_conversation_service import build_group_user_event
from character_memory.storage.message_search import MessageSearchRepository
from character_memory.storage.sqlite import SQLiteStore


def test_message_search_does_not_search_derived_memory(tmp_path):
    store = SQLiteStore(tmp_path / "source-of-truth.db")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    store.add_memory(
        Memory(
            character_id="rin",
            content="DERIVED-ONLY-NEEDLE",
            memory_type="SHARED",
            event_time=now,
            importance=0.9,
            source_event_id=None,
            metadata={"origin":"test"},
            embedding=[0.1, 0.2],
        )
    )

    repository = MessageSearchRepository(store)
    assert repository.search_direct("DERIVED-ONLY-NEEDLE", character_id="rin", limit=10) == []
    store.close()


def test_group_search_jump_cursor_keeps_old_hit_in_existing_history_window(tmp_path):
    store = SQLiteStore(tmp_path / "group-jump.db")
    groups = GroupRepository(store)
    start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    group = groups.create_group("深历史群", ["rin", "momo"], start)
    target = None

    for index in range(80):
        event = groups.append_event(
            build_group_user_event(
                group.id,
                f"group-message-{index}{' GROUP-NEEDLE' if index == 13 else ''}",
                at=start + timedelta(minutes=index),
            )
        )
        if index == 13:
            target = event

    search = MessageSearchRepository(store)
    hit = search.search_group("GROUP-NEEDLE", conversation_id=group.id, limit=10)[0]
    before_id = search.jump_before_group(group.id, hit["event_id"])
    assert before_id is not None

    page = groups.list_event_page(group.id, limit=50, before_id=before_id)
    assert target is not None
    assert target.id in [event.id for event in page.events]
    store.close()
