from datetime import datetime, timezone
from character_memory.storage.sqlite import SQLiteStore
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.domain.models import Memory

def test_vector_memory_roundtrip(tmp_path):
    store=SQLiteStore(tmp_path/'t.db'); emb=DeterministicEmbedding()
    m=Memory(character_id='rin',content='用户下午有一个重要汇报',event_time=datetime.now(timezone.utc),embedding=emb.embed('用户下午有一个重要汇报'))
    store.add_memory(m)
    got=VectorRecall(store,emb).recall('rin','下午汇报怎么样',1)
    assert got and '汇报' in got[0].content
