from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from character_memory.domain.models import Memory
from character_memory.memory.recall import VectorRecall
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026,10,25,tzinfo=timezone.utc)


class QueryEmbedding:
    def __init__(self, vector=None):
        self.vector = [1.,0.] if vector is None else vector
    def embed(self, text):
        return self.vector


@pytest.fixture
def store(tmp_path):
    value=SQLiteStore(tmp_path/'recall.db')
    try:
        yield value
    finally:
        value.close()


def add(store, text, *, kind='EPISODIC', cosine=.45, old=False, character='rin', **kwargs):
    values=dict(character_id=character,content=text,memory_type=kind,
        event_time=NOW-timedelta(days=90) if old else NOW,
        importance=.6 if old else .95,embedding=[cosine,sqrt(1-cosine*cosine)])
    values.update(kwargs)
    return store.add_memory(Memory(**values))


@pytest.mark.parametrize('kind',['USER','SHARED'])
def test_old_semantically_best_relationship_survives_recent_noise(store,kind):
    target=add(store,'old agreement',kind=kind,cosine=.6,old=True)
    with store.transaction():
        for i in range(32):
            add(store,f'recent space {i}')
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',now=NOW)
    assert target.id in [m.id for m in result]
    assert len(result)==8


@pytest.mark.parametrize('kind',['USER','SHARED'])
def test_recall_adds_relationship_candidates_without_expanding_default_admission(store,kind):
    target=add(store,'old user fact',kind=kind,cosine=.9,old=True)
    with store.transaction():
        for i in range(1100):
            add(store,f'noise {i}')
    assert target.id not in [m.id for m in store.list_memory_candidates('rin',at=NOW)]
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',now=NOW)
    assert target.id in [m.id for m in result]
    assert len(result)==8


def test_relationship_branch_is_bounded_unique_and_respects_ownership_state_time(store):
    first=add(store,'first',kind='USER',old=True,pinned=True)
    second=add(store,'second',kind='shared',old=True)
    add(store,'third',kind='USER',old=True,importance=.4)
    add(store,'foreign',kind='USER',character='other')
    add(store,'forgotten',kind='USER',active=False)
    add(store,'future',kind='USER',event_time=NOW+timedelta(days=1))
    newest=add(store,'recent noise')
    rows=store.list_memory_candidates('rin',at=NOW,recent_limit=1,important_limit=1,relationship_limit=2)
    assert {m.id for m in rows}=={first.id,second.id,newest.id}
    assert len(rows)==len({m.id for m in rows})


def test_unrelated_relationship_does_not_displace_more_relevant_experiences(store):
    unrelated=add(store,'unrelated user',kind='USER',cosine=.1,old=True)
    expected=[add(store,f'actual observation {i}',kind='SELF',cosine=.9).id for i in range(8)]
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',now=NOW)
    assert [m.id for m in result]==expected
    assert unrelated.id not in [m.id for m in result]


@pytest.mark.parametrize('limit',[1,2,4,8])
def test_protection_is_at_most_two_and_honors_result_limit(store,limit):
    relations=[add(store,f'relation {i}',kind='SHARED',cosine=cos,old=True)
        for i,cos in enumerate([.9,.8,.7])]
    noise=[add(store,f'noise {i}',cosine=.6) for i in range(8)]
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',limit,now=NOW)
    ids=[m.id for m in result]
    assert len(result)==limit and len(ids)==len(set(ids))
    if limit==1:
        assert ids==[relations[0].id]
    if limit==4:
        assert relations[0].id in ids and relations[1].id in ids
        assert relations[2].id not in ids
        assert sum(m.id in ids for m in noise)==2


def test_zero_query_does_not_promote_relationship_metadata(store):
    target=add(store,'old',kind='USER',old=True)
    noise=[add(store,f'fresh {i}') for i in range(8)]
    result=VectorRecall(store,QueryEmbedding([0.,0.])).recall('rin','query',now=NOW)
    assert target.id not in [m.id for m in result]
    assert [m.id for m in result]==[m.id for m in noise]


def test_bad_vectors_future_and_inactive_never_become_protected(store):
    add(store,'wrong dimension',kind='USER',embedding=[1.,0.,0.])
    add(store,'no vector',kind='USER',embedding=[])
    add(store,'future',kind='USER',event_time=NOW+timedelta(days=1),cosine=1.)
    add(store,'inactive',kind='USER',active=False,cosine=1.)
    valid=add(store,'valid',kind='USER',old=True,cosine=.6)
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',now=NOW)
    assert [m.id for m in result]==[valid.id]


def test_without_relationship_candidates_existing_stable_score_order_is_unchanged(store):
    expected=[add(store,f'tie {i}',cosine=.5).id for i in range(4)]
    result=VectorRecall(store,QueryEmbedding()).recall('rin','query',3,now=NOW)
    assert [m.id for m in result]==expected[:3]
