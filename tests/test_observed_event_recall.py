from datetime import datetime, timedelta, timezone

import pytest

from character_memory.domain.models import Event, EventType
from character_memory.runtime.context import compile_context
from character_memory.runtime.person_context import PersonContextBuilder
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)


class LocalRecall:
    def __init__(self):
        self.calls = 0

    def recall(self, character_id, query, now):
        self.calls += 1
        return []


@pytest.fixture
def store(tmp_path):
    value = SQLiteStore(tmp_path / 'observations.db')
    try:
        yield value
    finally:
        value.close()


def observe(store, *, character='a', at=NOW, content='读过长期记忆文章后，我想继续了解记忆治理。', channel='PERSONAL_BROWSE'):
    return store.append_event(Event(character_id=character, event_type=EventType.WORLD_OBSERVATION, event_time=at, content=content, metadata={'channel': channel, 'world_summary': '长期记忆与 Agent Memory 架构', 'sources': ['https://example.com/memory']}))


@pytest.mark.parametrize("query", ["长期记忆", "你上次读过的长期记忆文章是什么"])
def test_observation_survives_thirty_ordinary_events_without_extra_model_work(store, query):
    event = observe(store)
    for index in range(30):
        store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW + timedelta(minutes=index+1), content='普通聊天'))
    recall = LocalRecall()
    snapshot = PersonContextBuilder(store, recall, 'persona').build('a', query=query, at=NOW + timedelta(hours=1))
    assert event.id not in [row.id for row in snapshot.recent_events]
    assert [row.id for row in snapshot.observed_events] == [event.id]
    assert snapshot.observed_events[0].metadata['sources'] == ['https://example.com/memory']
    assert snapshot.observed_events[0].event_time == NOW
    assert recall.calls == 1
    assert store.list_memories('a') == []


def test_unread_non_observation_and_other_character_are_not_experiences(store):
    observe(store, character='a')
    store.append_event(Event(character_id='b', event_type=EventType.USER_MESSAGE, event_time=NOW, content='长期记忆文章已被RSS采集'))
    assert store.recall_observed_events('b', '长期记忆', at=NOW) == []
    assert store.recall_observed_events('a', '天气预报', at=NOW) == []
    assert store.recall_observed_events('a', '', at=NOW) == []


def test_observation_time_barrier_and_public_projection(store):
    private = observe(store)
    public = observe(store, channel='WORLD_PULSE')
    observe(store, at=NOW+timedelta(days=1), channel='WORLD_PULSE')
    personal = store.recall_observed_events('a', 'Agent Memory', at=NOW)
    assert {row.id for row in personal} == {private.id, public.id}
    visible = store.recall_observed_events('a', 'Agent Memory', at=NOW, projection='PUBLIC')
    assert [row.id for row in visible] == [public.id]


def test_supplied_recent_events_apply_time_and_exclusion(store):
    past = store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW-timedelta(minutes=1), content='过去'))
    future = store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW+timedelta(minutes=1), content='未来'))
    snapshot = PersonContextBuilder(store, LocalRecall(), 'persona').build('a', query='记忆', at=NOW, recent_events=[past, future], exclude_event_id=past.id)
    assert snapshot.recent_events == []


def test_matching_history_not_only_latest_candidate_window(store):
    target = observe(store, content='我读过关于紫杉醇药物研究的公开文章。')
    for index in range(100):
        observe(store, at=NOW+timedelta(seconds=index+1), content='今天观察了海洋生物。')
    result = store.recall_observed_events('a', '紫杉醇', at=NOW+timedelta(hours=1))
    assert [row.id for row in result] == [target.id]


def test_recall_and_prompt_are_bounded_without_mutating_provenance(store):
    for index in range(20):
        observe(store, at=NOW+timedelta(seconds=index), content='长期记忆' * 2000)
    snapshot = PersonContextBuilder(store, LocalRecall(), 'persona').build('a', query='长期记忆', at=NOW+timedelta(hours=1))
    assert len(snapshot.observed_events) == 4
    context = compile_context('persona', '', [], Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW+timedelta(hours=1), content='长期记忆'), observed_events=snapshot.observed_events)
    block = context.split('# Observed Experiences\n', 1)[1].split('\n#', 1)[0]
    assert len(block) <= 4000
    for event in snapshot.observed_events:
        assert f'event_id={event.id}' in block
        assert 'https://example.com/memory' in block
        assert NOW.date().isoformat() in block
        assert len(event.content) == 8000


def test_real_direct_reaction_receives_old_observation_with_one_reaction(store):
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime
    from test_character_architecture_baseline import CountingEmbedding, CountingModel

    observed = observe(store)
    for index in range(30):
        store.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW+timedelta(minutes=index+1), content='普通聊天'))
    embedding, model = CountingEmbedding(), CountingModel()
    runtime = PersonRuntime(store, VectorRecall(store, embedding), embedding, model, 'persona')
    result = runtime.handle(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=NOW+timedelta(hours=1), content='你上次读过的长期记忆文章是什么'))
    assert [row[0] for row in model.calls] == ['PersonReaction']
    assert f'event_id={observed.id}' in model.calls[0][2]
    assert 'https://example.com/memory' in result.context
    assert embedding.calls == 1
    assert result.created_memory_ids == []


def test_supplied_shared_facts_keep_actor_identity_without_personal_event_id(store):
    shared = Event(character_id='other', event_type=EventType.CHARACTER_MESSAGE, event_time=NOW, content='群聊中已公开表达')
    snapshot = PersonContextBuilder(store, LocalRecall(), 'persona').build('a', query='公开', at=NOW, recent_events=[shared], observed_projection='PUBLIC')
    assert snapshot.recent_events == [shared]
    assert shared.id is None


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", "42"])
@pytest.mark.parametrize("ordinary_events", [0, 30])
def test_legacy_invalid_metadata_preserves_fact_without_inventing_provenance(store, raw, ordinary_events):
    event = observe(store)
    store.conn.execute("UPDATE events SET metadata_json=? WHERE id=?", (raw, event.id))
    store.conn.commit()
    for index in range(ordinary_events):
        store.append_event(Event(character_id="a", event_type=EventType.USER_MESSAGE,
                                 event_time=NOW + timedelta(minutes=index+1), content="普通聊天"))
    snapshot = PersonContextBuilder(store, LocalRecall(), "persona").build(
        "a", query="长期记忆", at=NOW + timedelta(hours=1))
    observed = snapshot.observed_events[0]
    assert observed.id == event.id and observed.content == event.content and observed.event_time == event.event_time
    assert observed.metadata == {"metadata_status": "INVALID"}
    assert store.conn.execute("SELECT metadata_json FROM events WHERE id=?", (event.id,)).fetchone()[0] == raw
    assert store.recall_observed_events("a", "长期记忆", at=NOW, projection="PUBLIC") == []
    assert store.list_memories("a") == []
    rendered = compile_context(persona="persona", mental_state="", memories=[],
                               event=Event(character_id="a", event_type=EventType.USER_MESSAGE,
                                           event_time=NOW + timedelta(hours=1), content="长期记忆"),
                               observed_events=snapshot.observed_events)
    assert "sources=[]" in rendered and "https://example.com/memory" not in rendered
