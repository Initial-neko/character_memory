from datetime import datetime, timezone

from character_memory.domain.models import Memory
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)


def test_corrupt_memory_metadata_does_not_break_recall_reads(tmp_path):
    store = SQLiteStore(tmp_path / "memory.db")
    try:
        memory = store.add_memory(
            Memory(
                character_id="a",
                content="共同经历",
                event_time=NOW,
                importance=0.8,
                embedding=[1.0, 0.0],
            )
        )
        store.conn.execute(
            "UPDATE memories SET metadata_json=? WHERE id=?",
            ["{broken", memory.id],
        )
        store.conn.commit()

        reads = (
            store.list_memories("a"),
            store.list_memory_candidates("a", at=NOW),
        )
        for memories in reads:
            assert [item.id for item in memories] == [memory.id]
            assert memories[0].metadata == {"metadata_status": "INVALID"}

        found = store.find_active_memory_by_content("a", "共同经历", at=NOW)
        assert found is not None
        assert found.metadata == {"metadata_status": "INVALID"}
    finally:
        store.close()
