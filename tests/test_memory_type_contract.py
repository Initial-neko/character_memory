import json
from datetime import datetime, timezone

import httpx
import pytest

from character_memory.domain.models import Event, EventType, MemoryCandidate, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.context import compile_context
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore

KINDS = {'USER', 'SELF', 'SHARED', 'LIFE', 'DIARY', 'EPISODIC'}
NOW = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)


def test_candidate_schema_exposes_existing_taxonomy():
    schema = MemoryCandidate.model_json_schema()
    assert set(schema['properties']['memory_type']['enum']) == KINDS


@pytest.mark.parametrize('kind', sorted(KINDS))
def test_candidate_type_normalizes_case_and_whitespace(kind):
    candidate = MemoryCandidate(content='a real sourced detail', memory_type=f' {kind.lower()} ')
    assert candidate.memory_type == kind


@pytest.mark.parametrize('value', ['invented', 42, [], {}, [1], {'code':'USER'}, None, ''])
def test_unknown_annotation_does_not_discard_valid_outward_message(value):
    reaction = PersonReaction.model_validate({'actions':[{'type':'MESSAGE','message':'visible reply'}],
        'memory_candidates':[{'content':'retain valid content', 'memory_type':value, 'importance':.8}]})
    assert reaction.actions[0].message == 'visible reply'
    assert len(reaction.memory_candidates) == 1
    assert reaction.memory_candidates[0].content == 'retain valid content'
    assert reaction.memory_candidates[0].memory_type == 'EPISODIC'
    assert reaction.memory_candidates[0].importance == .8


@pytest.mark.parametrize('images', [False, True])
def test_provider_sends_compact_semantic_type_contract_without_extra_call(images):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        system = payload['messages'][0]['content']
        assert 'memory_type' in system
        assert all(kind in system for kind in KINDS)
        assert '约定' in system and '不代表已执行' in system
        assert '来源' in system and '他人' in system
        assert payload['response_format'] == {'type':'json_object'}
        assert '"properties"' not in system
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({
            'actions':[{'type':'MESSAGE','message':'still visible'}],
            'memory_candidates':[{'content':'valid content','memory_type':'unsupported'}]})}}]})
    model = OpenAICompatibleModel('test', model='text-test', vision_model='vision-test', attempts=2)
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        if images:
            result = model.react_call_with_images_for_session('current event', ['data:image/png;base64,AAAA'], 'test')
        else:
            result = model.react_call_for_session('current event', 'test')
        assert result.value.memory_candidates[0].memory_type == 'EPISODIC'
        assert result.value.actions[0].message == 'still visible'
        assert result.trace.attempt == 1
        assert len(requests) == 1
    finally:
        model.close()


def test_non_person_contract_is_unchanged():
    from pydantic import BaseModel
    class OtherResult(BaseModel):
        value: str
    assert 'memory_type' not in OpenAICompatibleModel._system_prompt(OtherResult)


def test_typed_memory_admission_restart_and_relationship_projection(tmp_path):
    path = tmp_path / 'types.db'
    store = SQLiteStore(path)
    embeddings = DeterministicEmbedding()
    model = OpenAICompatibleModel('test', attempts=1)
    responses = iter([
        ('user', '用户每次做项目汇报前希望先练习开场'),
        ('shared', '我们一起为蓝色鱼缸取名星港'),
        ('self', '我阅读远端文章后形成了新的植物研究关注'),
    ])
    calls = []
    def request(messages, **kwargs):
        calls.append(messages)
        kind, content = next(responses)
        return json.dumps({'actions':[], 'memory_candidates':[{
            'content':content, 'memory_type':kind, 'importance':.8}]}, ensure_ascii=False)
    model._request = request
    runtime = PersonRuntime(store, VectorRecall(store, embeddings), embeddings, model, 'persona')
    source_ids = []
    try:
        for index in range(3):
            source = store.append_event(Event(character_id='rin', event_type=EventType.USER_MESSAGE,
                event_time=NOW, content=f'actual source {index}'))
            source_ids.append(source.id)
            runtime.handle(source, persist_event=False)
        assert len(calls) == 3
    finally:
        model.close()
        store.close()
    reopened = SQLiteStore(path)
    try:
        memories = reopened.list_memories('rin')
        assert {m.memory_type for m in memories} == {'USER','SHARED','SELF'}
        assert {m.source_event_id for m in memories} == set(source_ids)
        assert all(m.event_time == NOW for m in memories)
        recall = VectorRecall(reopened, embeddings).recall('rin', '星港', now=NOW)
        assert len(recall) == 3
        for channel in ['DIRECT','GROUP']:
            event = Event(character_id='rin', event_type=EventType.USER_MESSAGE, event_time=NOW,
                content='继续上次的事', metadata={'channel':channel})
            prompt = compile_context('persona', '', recall, event)
            relationship = prompt.split('# Relationship Context',1)[1].split('#',1)[0]
            assert '[USER]' in relationship and '[SHARED]' in relationship
            assert '[SELF]' not in relationship
    finally:
        reopened.close()


def test_legacy_persisted_custom_type_remains_readable(tmp_path):
    from character_memory.domain.models import Memory
    path = tmp_path / 'legacy.db'
    store = SQLiteStore(path)
    store.add_memory(Memory(character_id='rin', content='legacy text',
        memory_type='legacy-custom', event_time=NOW))
    store.close()
    reopened = SQLiteStore(path)
    try:
        assert reopened.list_memories('rin')[0].memory_type == 'legacy-custom'
    finally:
        reopened.close()
