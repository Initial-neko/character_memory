from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from character_memory import cli
from character_memory.domain.models import Memory, MemoryCandidate
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def test_exact_duplicate_unicode_outside_shortlist(tmp_path):
    store = SQLiteStore(tmp_path / "unicode.db")
    try:
        with store.transaction():
            old = store.add_memory(Memory(character_id="rin", content="\u2003Straße\u2003",
                event_time=NOW - timedelta(days=2000), importance=0.1, embedding=[1, 0]))
            for index in range(1100):
                store.add_memory(Memory(character_id="rin", content=f"filler-{index}",
                    event_time=NOW - timedelta(seconds=index), importance=0.8, embedding=[0, 1]))
        assert old.id not in {m.id for m in store.list_memory_candidates("rin", at=NOW)}
        assert store.find_active_memory_by_content("rin", " STRASSE ", at=NOW).id == old.id
        def unexpected_embedding(_):
            pytest.fail("exact duplicates must not buy another embedding")
        runtime = PersonRuntime(store, None, SimpleNamespace(embed=unexpected_embedding), None, "test")
        accepted, decisions = runtime._prepare_memory_writes("rin", NOW,
            [MemoryCandidate(content="STRASSE", importance=0.8)])
        assert accepted == []
        assert decisions[0]["decision"] == "SKIP_DUPLICATE"
        assert decisions[0]["duplicate_memory_id"] == old.id
        assert runtime._active_memory_duplicate("rin", NOW, "STRASSE", None) == (old.id, 1.0)
        assert store.find_active_memory_by_content("other", "STRASSE", at=NOW) is None
        assert store.find_active_memory_by_content("rin", "STRASSE", at=old.event_time - timedelta(seconds=1)) is None
        store.set_memory_active(old.id, False)
        assert store.find_active_memory_by_content("rin", "STRASSE", at=NOW) is None
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["write", "short_batch", "long_batch", "none"])
def test_reembed_is_atomic_and_checks_batch_size(tmp_path, monkeypatch, failure):
    path = tmp_path / "reembed.db"
    store = SQLiteStore(path)
    for index in range(2):
        store.add_memory(Memory(character_id="rin", content=f"memory-{index}",
            event_time=NOW, embedding=[0, 1]))
    store.close()

    class FailingStore(SQLiteStore):
        updates = 0

        def update_memory_embedding(self, memory_id, vector):
            self.updates += 1
            if failure == "write" and self.updates == 2:
                raise OSError("injected write failure")
            return super().update_memory_embedding(memory_id, vector)

    count = {"short_batch": 1, "long_batch": 3}.get(failure, 2)
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(db_path=str(path)))
    monkeypatch.setattr(cli, "build_embedding", lambda _: SimpleNamespace(embed_many=lambda _: [[1, 0]] * count))
    monkeypatch.setattr(cli, "SQLiteStore", FailingStore)
    if failure == "none":
        cli._reembed("unused", "rin")
    else:
        with pytest.raises(OSError if failure == "write" else ValueError):
            cli._reembed("unused", "rin")
    reopened = SQLiteStore(path)
    try:
        expected = [1, 0] if failure == "none" else [0, 1]
        assert [m.embedding for m in reopened.list_memories("rin")] == [expected, expected]
    finally:
        reopened.close()


def test_pins_can_exceed_unpinned_bound_but_not_final_recall_limit(tmp_path):
    from character_memory.memory.recall import VectorRecall

    store = SQLiteStore(tmp_path / "pins.db")
    try:
        with store.transaction():
            for index in range(1030):
                store.add_memory(Memory(character_id="rin", content=f"pin-{index}",
                    event_time=NOW, pinned=True, embedding=[1, 0]))
            store.add_memory(Memory(character_id="rin", content="future pin",
                event_time=NOW + timedelta(days=1), pinned=True, embedding=[1, 0]))
        candidates = store.list_memory_candidates("rin", at=NOW)
        assert len(candidates) == 1030
        assert len({m.id for m in candidates}) == 1030
        recall = VectorRecall(store, SimpleNamespace(embed=lambda _: [1, 0]))
        assert len(recall.recall("rin", "query", now=NOW)) == 8
    finally:
        store.close()
