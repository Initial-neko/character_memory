from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from character_memory.domain.models import Event, EventType
from character_memory.runtime.person_context import PersonContextBuilder
from character_memory.storage.sqlite import SQLiteStore

NOW = datetime(2026, 10, 25, 8, tzinfo=timezone.utc)


@pytest.fixture
def history(tmp_path):
    store = SQLiteStore(tmp_path / 'recent.db')
    facts = []
    with store.transaction():
        for index in range(12):
            facts.append(store.append_message(Event(character_id='rin', event_type=EventType.USER_MESSAGE,
                event_time=NOW + timedelta(seconds=index), content=f'shared-experience-{index}')))
            # A burst of receipts must not hide the preceding actual facts.
            for receipt in range(4):
                store.append_event(Event(character_id='rin', event_type=EventType.ACTION,
                    event_time=NOW + timedelta(seconds=index), content='INTERNAL_RECEIPT',
                    metadata={'trace_id': index * 4 + receipt + 1}))
    try:
        yield store, facts
    finally:
        store.close()


def builder(store):
    return PersonContextBuilder(store, SimpleNamespace(recall=lambda *a, **kw: []), 'persona')


def test_actual_facts_fill_window_before_limit_and_audit_stays_intact(history):
    store, facts = history
    before = store.conn.total_changes
    snapshot = builder(store).build('rin', query='shared', at=NOW + timedelta(minutes=1))
    assert [e.id for e in snapshot.recent_events] == [e.id for e in facts[-8:]]
    assert len(store.list_events('rin', limit=100)) == 60
    assert store.conn.total_changes == before


@pytest.mark.parametrize('supplied', [False, True])
def test_time_identity_exclusion_and_real_action_projection(history, supplied):
    store, facts = history
    collaboration = store.append_event(Event(character_id='rin', event_type=EventType.ACTION,
        event_time=NOW + timedelta(seconds=20), content='real collaboration',
        metadata={'trace_id': 100, 'fact_kind': 'ROLE_COLLABORATION_MESSAGE'}))
    unknown = store.append_event(Event(character_id='rin', event_type=EventType.ACTION,
        event_time=NOW + timedelta(seconds=21), content='unknown historical action'))
    store.append_message(Event(character_id='other', event_type=EventType.USER_MESSAGE,
        event_time=NOW + timedelta(seconds=22), content='another person'))
    future = store.append_message(Event(character_id='rin', event_type=EventType.USER_MESSAGE,
        event_time=NOW + timedelta(days=1), content='future'))
    kwargs = {'recent_events': store.list_events('rin', limit=100)} if supplied else {}
    snapshot = builder(store).build('rin', query='shared', at=NOW + timedelta(minutes=1),
        exclude_event_id=facts[-1].id, recent_limit=4, **kwargs)
    assert [e.id for e in snapshot.recent_events] == [facts[-3].id, facts[-2].id, collaboration.id, unknown.id]
    assert future.id not in [e.id for e in snapshot.recent_events]


@pytest.mark.parametrize('channel', ['DIRECT', 'SPACE', 'WORLD'])
def test_real_channel_prompt_paths_receive_actual_window(history, channel):
    store, facts = history
    context_builder = builder(store)
    runtime = SimpleNamespace(context_builder=context_builder)
    at = NOW + timedelta(minutes=1)
    if channel == 'SPACE':
        from character_memory.space_autonomy import SpaceAutonomyService
        service = SpaceAutonomyService.__new__(SpaceAutonomyService)
        service.access = SimpleNamespace(settings=SimpleNamespace(space_media_enabled=False))
        prompt = service._daily_context('rin', at, runtime)
    else:
        snapshot = context_builder.build('rin', query='shared', at=at)
        if channel == 'DIRECT':
            from character_memory.runtime.context import compile_context
            event = Event(character_id='rin', event_type=EventType.USER_MESSAGE, event_time=at, content='next')
            prompt = compile_context('persona', '', [], event, recent_events=snapshot.recent_events)
        else:
            from character_memory.world_continuation import WorldContinuation
            service = WorldContinuation.__new__(WorldContinuation)
            service.service = SimpleNamespace(_memory_text=lambda c: '- none')
            prompt = service._prompt(snapshot, [], [], [], 1)
    assert 'INTERNAL_RECEIPT' not in prompt
    assert 'shared-experience-4' in prompt
    assert 'shared-experience-11' in prompt


def test_supplied_group_facts_unchanged(history):
    store, _ = history
    facts = [Event(character_id='other', event_type=EventType.CHARACTER_MESSAGE,
        event_time=NOW, content='actual shared group reply', metadata={'channel': 'GROUP'})]
    snapshot = builder(store).build('rin', query='shared', at=NOW, recent_events=facts)
    assert snapshot.recent_events == facts

def test_real_chat_api_reuses_actual_history_after_restart(tmp_path):
    from fastapi.testclient import TestClient
    from character_memory.api import create_api
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
    from character_memory.llm.client import PersonModel
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime

    class RecordingModel(PersonModel):
        def __init__(self):
            self.prompts = []

        def react(self, context):
            self.prompts.append(context)
            return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message='ack')])

    db = tmp_path / 'api.db'
    model = RecordingModel()
    clock = FixedClock(NOW)

    def application(store):
        embeddings = DeterministicEmbedding()
        runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, 'persona')
        chat = ChatService(store, runtime, clock)
        settings = SimpleNamespace(chat_model='offline-recorder', embedding_provider='deterministic',
            embedding_model='deterministic', db_path=str(db), base_url='offline')
        return create_api(bundle=SimpleNamespace(settings=settings, chat=chat, store=store, clock=clock, runtime=runtime))

    store = SQLiteStore(db)
    with TestClient(application(store)) as client:
        for index in range(4):
            response = client.post('/v1/chat', json={'message': f'api-shared-{index}',
                'character_id': 'rin', 'conversation_id': 'shared'})
            assert response.status_code == 200
    receipts = [row.id for row in store.list_events('rin') if row.event_type == EventType.ACTION]
    assert len(receipts) == 4
    store.close()

    reopened = SQLiteStore(db)
    try:
        with TestClient(application(reopened)) as client:
            response = client.post('/v1/chat', json={'message': 'continue',
                'character_id': 'rin', 'conversation_id': 'shared'})
            assert response.status_code == 200
            history = client.get('/v1/chat/history', params={'character_id': 'rin', 'conversation_id': 'shared'})
            assert history.status_code == 200
            assert len(history.json()['messages']) == 10
        prompt = model.prompts[-1]
        assert all(f'api-shared-{index}' in prompt for index in range(4))
        assert ' ACTION ' not in prompt
        assert len(model.prompts) == 5
        assert all(reopened.get_event(row_id).event_type == EventType.ACTION for row_id in receipts)
        assert len([row for row in reopened.list_events('rin') if row.event_type == EventType.ACTION]) == 5
    finally:
        reopened.close()
