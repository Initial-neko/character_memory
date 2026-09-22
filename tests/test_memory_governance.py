from datetime import datetime, timezone

from character_memory.domain.models import Event, EventType, Memory
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_context import PersonContextBuilder
from character_memory.storage.sqlite import SQLiteStore


def test_memory_governance_pin_forget_restore_and_supersede(tmp_path):
    store = SQLiteStore(tmp_path / "memory-governance.db")
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    original = store.add_memory(
        Memory(
            character_id="momo",
            content="用户喜欢红茶",
            memory_type="USER",
            event_time=now,
            importance=0.6,
            embedding=[1.0, 0.0, 0.0],
        )
    )

    pinned = store.set_memory_pinned(original.id, True)
    assert pinned.active is True
    assert pinned.pinned is True

    forgotten = store.set_memory_active(original.id, False)
    assert forgotten.active is False
    assert forgotten.pinned is False

    restored = store.set_memory_active(original.id, True)
    assert restored.active is True

    replacement = Memory(
        character_id="momo",
        content="用户不喜欢红茶，更喜欢绿茶",
        memory_type="USER",
        event_time=now,
        importance=0.8,
        pinned=True,
        metadata={"governance": "CORRECTED", "corrects_memory_id": original.id},
        embedding=[0.0, 1.0, 0.0],
    )
    old, saved = store.supersede_memory(original.id, replacement)
    assert old.active is False
    assert old.superseded_by == saved.id
    assert saved.active is True
    assert saved.pinned is True
    assert "core/007-memory-governance" in store.list_schema_migrations()
    store.close()


def test_pinned_memory_stays_in_bounded_recall_candidates(tmp_path):
    store = SQLiteStore(tmp_path / "memory-candidates.db")
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    pinned = store.add_memory(
        Memory(
            character_id="momo",
            content="很久以前但必须保留的重要共同经历",
            event_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
            importance=0.1,
            pinned=True,
            embedding=[1.0, 0.0],
        )
    )
    for index in range(6):
        store.add_memory(
            Memory(
                character_id="momo",
                content=f"recent-{index}",
                event_time=now,
                importance=0.9,
                embedding=[0.0, 1.0],
            )
        )

    ids = {item.id for item in store.list_memory_candidates("momo", recent_limit=1, important_limit=1)}
    assert pinned.id in ids
    store.close()


def test_person_context_builder_reads_one_person_state_memory_and_recent_events(tmp_path):
    store = SQLiteStore(tmp_path / "person-context.db")
    embeddings = DeterministicEmbedding()
    recall = VectorRecall(store, embeddings, limit=4)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    store.set_mental_state("momo", "今天比较安静。", now)
    memory = Memory(
        character_id="momo",
        content="用户喜欢一起聊鱼缸。",
        memory_type="SHARED",
        event_time=now,
        importance=0.9,
        embedding=embeddings.embed("用户喜欢一起聊鱼缸。"),
    )
    store.add_memory(memory)
    event = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.USER_MESSAGE,
            event_time=now,
            content="今天看看鱼缸吧",
        )
    )

    snapshot = PersonContextBuilder(store, recall, "persona momo").build(
        "momo",
        query="鱼缸",
        at=now,
        recent_limit=4,
    )

    assert snapshot.persona == "persona momo"
    assert snapshot.mental_state == "今天比较安静。"
    assert any("鱼缸" in item.content for item in snapshot.memories)
    assert [item.id for item in snapshot.recent_events] == [event.id]
    store.close()
