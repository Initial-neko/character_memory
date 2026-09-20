from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest
from character_memory.storage.sqlite import SQLiteStore
from character_memory.memory.embedding import DeterministicEmbedding, SentenceTransformerEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.domain.models import Memory

def test_vector_memory_roundtrip(tmp_path):
    store=SQLiteStore(tmp_path/'t.db'); emb=DeterministicEmbedding()
    m=Memory(character_id='rin',content='用户下午有一个重要汇报',event_time=datetime.now(timezone.utc),embedding=emb.embed('用户下午有一个重要汇报'))
    store.add_memory(m)
    got=VectorRecall(store,emb).recall('rin','下午汇报怎么样',1)
    assert got and '汇报' in got[0].content



def test_embedding_runtime_is_strict_local_only(monkeypatch):
    calls = []
    monkeypatch.delenv("HF_HOME", raising=False)

    class FakeSentenceTransformer:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))
            raise OSError("cache miss")

    module = ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    with pytest.raises(RuntimeError, match="local cache"):
        SentenceTransformerEmbedding("BAAI/bge-small-zh-v1.5")

    assert calls == [
        ("BAAI/bge-small-zh-v1.5", {"local_files_only": True})
    ]
    assert Path(os.environ["HF_HOME"]).as_posix().endswith("models/huggingface")


def test_memory_candidate_set_is_bounded_but_keeps_old_important_memory(tmp_path):
    store = SQLiteStore(tmp_path / "bounded.db")
    emb = DeterministicEmbedding()
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)

    old_important = store.add_memory(
        Memory(
            character_id="rin",
            content="非常重要的长期约定",
            event_time=start,
            importance=1.0,
            embedding=emb.embed("非常重要的长期约定"),
        )
    )
    for index in range(1100):
        store.add_memory(
            Memory(
                character_id="rin",
                content=f"普通记忆 {index}",
                event_time=start + timedelta(days=index + 1),
                importance=0.1,
                embedding=emb.embed(f"普通记忆 {index}"),
            )
        )

    candidates = store.list_memory_candidates(
        "rin",
        at=start + timedelta(days=2000),
        recent_limit=768,
        important_limit=256,
    )

    assert len(candidates) <= 1024
    assert any(item.id == old_important.id for item in candidates)
    store.close()
