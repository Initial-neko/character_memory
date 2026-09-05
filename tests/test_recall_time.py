from datetime import datetime, timedelta, timezone

from character_memory.domain.models import Memory
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.storage.sqlite import SQLiteStore


def test_recall_never_reads_future_memory(tmp_path):
    store = SQLiteStore(tmp_path / "t.db")
    emb = DeterministicEmbedding()
    now = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
    past = Memory(
        character_id="rin",
        content="上午聊了项目",
        event_time=now - timedelta(hours=1),
        embedding=emb.embed("上午聊了项目"),
    )
    future = Memory(
        character_id="rin",
        content="晚上去了书店",
        event_time=now + timedelta(hours=8),
        embedding=emb.embed("晚上去了书店"),
    )
    store.add_memory(past)
    store.add_memory(future)
    recalled = VectorRecall(store, emb).recall("rin", "书店 项目", 10, now=now)
    assert past.id is None
    assert [m.content for m in recalled] == ["上午聊了项目"]
