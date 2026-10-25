import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from character_memory.domain.models import Event, EventType, PersonReaction
from character_memory.eval.runner import EvalRunner
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.context import compile_context_parts
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore

NOW=datetime(2026,10,25,10,tzinfo=timezone.utc)


@pytest.mark.parametrize('channel',['DIRECT','GROUP'])
def test_policy_preserves_modality_and_allows_sourced_completion_and_quotes(channel):
    event=Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,
        content='what do you remember?',metadata={'channel':channel})
    policy=compile_context_parts('persona','',[],event).runtime_policy
    assert '偏好、愿望' in policy
    assert '逐字原话' in policy and '记忆摘要' in policy
    assert '明确记录已完成' in policy and '可以' in policy
    assert '可见原文' in policy
    assert '不要求旧记忆' in policy
    assert '未记录某项细节' in policy and '否定事实' in policy


class RecordingModel(PersonModel):
    def __init__(self):
        self.prompts=[]
    def react(self,context):
        self.prompts.append(context)
        return PersonReaction(actions=[])


def test_eval_seeds_sourced_memory_without_extra_reaction_and_preserves_review_gate(tmp_path):
    store=SQLiteStore(tmp_path/'eval.db')
    embeddings=DeterministicEmbedding()
    model=RecordingModel()
    runtime=PersonRuntime(store,VectorRecall(store,embeddings),embeddings,model,'persona')
    case={'id':'pref','character_id':'rin','event_time':NOW.isoformat(),'content':'开场怎么准备？',
        'seed_events':[{'event_type':'USER_MESSAGE','event_time':'2026-07-27T10:00:00+00:00',
            'content':'我希望先练开场前三句','memories':[{'content':'用户希望先练开场前三句','memory_type':'USER','importance':.8}]}],
        'review_criteria':['偏好不能升级成已完成的往事'],
        'context_contains':['用户希望先练开场前三句']}
    path=tmp_path/'case.jsonl'
    path.write_text(json.dumps(case,ensure_ascii=False),encoding='utf-8')
    try:
        result=EvalRunner(runtime).run_jsonl(path)[0]
        assert result['pass'] is True
        assert result['review_required'] is True
        assert result['review_criteria']==case['review_criteria']
        assert len(model.prompts)==1
        source=store.get_event(result['seed_event_ids'][0])
        memory=store.get_memory(result['seed_memory_ids'][0])
        assert memory.source_event_id==source.id
        assert memory.event_time==source.event_time
        assert memory.character_id==source.character_id=='rin'
        assert memory.memory_type=='USER'
        assert memory.id in result['recalled_memory_ids']
    finally:
        store.close()


def test_seed_embedding_failure_does_not_leave_partial_fixture(tmp_path):
    store=SQLiteStore(tmp_path/'failure.db')
    class FailingEmbedding(DeterministicEmbedding):
        def embed(self,text):
            raise RuntimeError('test embedding unavailable')
    embeddings=FailingEmbedding()
    runtime=PersonRuntime(store,VectorRecall(store,embeddings),embeddings,RecordingModel(),'persona')
    path=tmp_path/'bad.jsonl'
    path.write_text(json.dumps({'id':'bad','event_time':NOW.isoformat(),'content':'query',
        'seed_events':[{'event_type':'USER_MESSAGE','event_time':NOW.isoformat(),'content':'source',
            'memories':[{'content':'derived fact'}]}]}),encoding='utf-8')
    try:
        with pytest.raises(RuntimeError,match='embedding unavailable'):
            EvalRunner(runtime).run_jsonl(path)
        assert store.list_events('rin')==[]
        assert store.list_memories('rin')==[]
    finally:
        store.close()


def test_new_suite_contains_all_four_reviewable_control_cases():
    cases=[json.loads(line) for line in Path('evals/memory_modality.jsonl').read_text(encoding='utf-8').splitlines()]
    assert {case['id'] for case in cases}=={'memory_preference','memory_future_agreement','memory_completed_fact','memory_visible_quote'}
    assert all(case['review_criteria'] for case in cases)
    assert all(case.get('min_messages')==1 for case in cases)
    assert 'seed_events' in cases[0]

@pytest.mark.parametrize('criteria',['not-a-list',[42]])
def test_invalid_review_criteria_fails_before_model_or_fixture_writes(tmp_path,criteria):
    store=SQLiteStore(tmp_path/'invalid-review.db')
    embeddings=DeterministicEmbedding()
    model=RecordingModel()
    runtime=PersonRuntime(store,VectorRecall(store,embeddings),embeddings,model,'persona')
    path=tmp_path/'invalid.jsonl'
    path.write_text(json.dumps({'id':'invalid','event_time':NOW.isoformat(),'content':'query',
        'review_criteria':criteria,'seed_events':[{'event_time':NOW.isoformat(),'content':'source'}]}),encoding='utf-8')
    try:
        with pytest.raises(ValueError,match='review_criteria'):
            EvalRunner(runtime).run_jsonl(path)
        assert not model.prompts and store.list_events('rin')==[]
    finally:
        store.close()


def test_legacy_case_keeps_output_shape_and_needs_no_fixture_interface(tmp_path):
    from test_review_regressions import ResourceEvalRuntime
    path=tmp_path/'legacy.jsonl'
    path.write_text(json.dumps({'id':'legacy','event_time':NOW.isoformat(),'content':'image'}),encoding='utf-8')
    result=EvalRunner(ResourceEvalRuntime()).run_jsonl(path)[0]
    assert result['pass'] is True and result['actions']==['IMAGE']
    assert 'seed_event_ids' not in result and 'review_required' not in result
