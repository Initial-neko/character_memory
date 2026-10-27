"""User-visible reply suggestions: no character-private context or implicit sending."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event, EventType
from character_memory.domain.facts import FactRef
from character_memory.storage.experience_profiles import ExperienceProfileRepository

NOW=datetime.now(timezone.utc)-timedelta(seconds=2)
CHOICES=[{'intent':'澄清','text':'你更在意哪一部分？'},{'intent':'表达感受','text':'我有一点紧张，想慢慢来。'},{'intent':'提出计划','text':'我们先练习一个开场吧。'}]

class Model:
    def __init__(self,hook=None,fail=False):self.calls=0;self.prompts=[];self.hook=hook;self.fail=fail
    def structured_call_for_session(self,prompt,schema,session):
        self.calls+=1;self.prompts.append(prompt)
        if self.hook:self.hook()
        if self.fail:raise RuntimeError('provider unavailable')
        return SimpleNamespace(value=schema.model_validate({'suggestions':CHOICES}))

def setup(store,model=None):
    from character_memory.application.reply_suggestions import ReplySuggestionService
    return ReplySuggestionService(store,persona_for=lambda char:'Persona for '+char,model_for=lambda:model or Model(),ensure_character=lambda char:{'id':char})

def event(store,content='明天有聚餐',character='rin',**metadata):
    return store.append_event(Event(character_id=character,event_type=EventType.CHARACTER_MESSAGE,event_time=NOW,content=content,metadata=metadata))

def request(source,**changes):
    from character_memory.domain.reply_suggestions import SuggestionRequest
    return SuggestionRequest(request_id=uuid4(),context_ref=FactRef(kind='EVENT',id=source.id),expected_profile_version=0,**changes)

@pytest.mark.parametrize('choices',[CHOICES[:2],CHOICES*2,[CHOICES[0],CHOICES[0],CHOICES[1]],[{'intent':'x','text':'  '},*CHOICES[:2]]])
def test_output_rejects_count_duplicates_or_empty(choices):
    from character_memory.domain.reply_suggestions import SuggestionOutput
    with pytest.raises(ValueError):SuggestionOutput(suggestions=choices)

def test_idempotent_batch_and_get_never_send_or_change_persona_state(tmp_path):
    store=SQLiteStore(tmp_path/'a.db');source=event(store);model=Model();service=setup(store,model);req=request(source)
    first=service.generate(req);second=service.generate(req)
    assert first==second and first['status']=='READY' and len(first['suggestions'])==3 and model.calls==1
    assert service.get(first['batch_id'])==first and model.calls==1
    assert len(store.list_chat_events('rin'))==1 and store.get_mental_state('rin')==''
    with pytest.raises(Exception):service.generate(req.model_copy(update={'expected_profile_version':1}))
    assert model.calls==1;store.close()

def test_private_memory_and_mental_state_are_not_loaded(tmp_path):
    store=SQLiteStore(tmp_path/'private.db');source=event(store);model=Model();service=setup(store,model)
    store.get_mental_state=lambda *a,**k:(_ for _ in ()).throw(AssertionError('private state read'))
    service.generate(request(source))
    assert '明天有聚餐' in model.prompts[0] and 'Persona for rin' in model.prompts[0]
    assert '用户' in model.prompts[0] and '3' in model.prompts[0];store.close()

@pytest.mark.parametrize('mutation',['profile','message','hidden'])
def test_batch_invalidates_after_profile_or_context_change(tmp_path,mutation):
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'stale.db');source=event(store);service=setup(store);batch=service.generate(request(source))
    if mutation=='profile':ExperienceProfileRepository(store).patch(0,{'tone':'CUTE'})
    elif mutation=='message':event(store,'更新的消息')
    else:
        store.conn.execute('UPDATE events SET metadata_json=? WHERE id=?',(json.dumps({'hidden':True}),source.id));store.conn.commit()
    with pytest.raises(SuggestionStale):service.get(batch['batch_id'])
    store.close()

def test_changes_while_model_runs_are_not_published(tmp_path):
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'during.db');source=event(store);model=Model(hook=lambda:ExperienceProfileRepository(store).patch(0,{'tone':'CUTE'}));service=setup(store,model)
    with pytest.raises(SuggestionStale):service.generate(request(source))
    assert model.calls==1;store.close()

def test_failed_request_does_not_repeat_after_reopen(tmp_path):
    from character_memory.application.reply_suggestions import SuggestionUnavailable
    path=tmp_path/'failed.db';store=SQLiteStore(path);source=event(store);model=Model(fail=True);service=setup(store,model);req=request(source)
    with pytest.raises(SuggestionUnavailable):service.generate(req)
    store.close();store=SQLiteStore(path);service=setup(store,model)
    with pytest.raises(SuggestionUnavailable):service.generate(req)
    assert model.calls==1;store.close()

def test_two_connections_only_one_claims_model_work(tmp_path):
    path=tmp_path/'race.db';stores=[SQLiteStore(path),SQLiteStore(path)];source=event(stores[0]);started=threading.Event();release=threading.Event()
    model=Model(hook=lambda:(started.set(),release.wait(8)));services=[setup(s,model) for s in stores];req=request(source)
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(services[0].generate,req);assert started.wait(5)
        pending=services[1].generate(req);assert pending['status']=='PENDING';release.set();ready=future.result()
    assert model.calls==1 and ready['batch_id']==pending['batch_id']
    for s in stores:s.close()

def test_group_membership_scope_and_same_numeric_domains(tmp_path):
    from character_memory.group_store import GroupRepository,GroupEvent
    from character_memory.domain.reply_suggestions import SuggestionRequest
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'group.db');direct=event(store,'私聊秘密');repo=GroupRepository(store);group=repo.create_group('Group',['rin','momo'],NOW)
    source=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='t',actor_type='CHARACTER',actor_id='rin',event_type='CHARACTER_MESSAGE',event_time=NOW,content='共同可见'))
    model=Model();service=setup(store,model);req=SuggestionRequest(request_id=uuid4(),context_ref=FactRef(kind='CONVERSATION_EVENT',id=source.id,conversation_id=group.id),expected_profile_version=0)
    batch=service.generate(req);assert '共同可见' in model.prompts[0] and '私聊秘密' not in model.prompts[0]
    repo.archive_group(group.id,NOW)
    with pytest.raises(SuggestionStale):service.get(batch['batch_id'])
    assert model.calls==1;store.close()


@pytest.mark.parametrize('kind',['hidden','simulation','future','stale'])
def test_invisible_or_old_source_never_calls_model(tmp_path,kind):
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'deny.db');source=event(store);model=Model();service=setup(store,model)
    if kind=='hidden':
        store.conn.execute('UPDATE events SET metadata_json=? WHERE id=?',(json.dumps({'hidden':True}),source.id))
    elif kind=='simulation':
        store.conn.execute('UPDATE events SET metadata_json=? WHERE id=?',(json.dumps({'experience_scope':{'schema_version':1,'reality':'SIMULATION'}}),source.id))
    elif kind=='future':
        store.conn.execute('UPDATE events SET event_time_epoch=? WHERE id=?',(2**63-1,source.id))
    else:event(store,'new message')
    store.conn.commit()
    with pytest.raises(SuggestionStale):service.generate(request(source))
    assert model.calls==0;store.close()


def test_http_success_pending_cache_validation_and_read_without_model(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.reply_suggestions_web import attach_reply_suggestion_routes
    store=SQLiteStore(tmp_path/'http.db');source=event(store);model=Model();app=FastAPI()
    access=SimpleNamespace(read_store=store,ensure_character=lambda char:{'id':char},settings=None,require_bundle=lambda:SimpleNamespace(model=model))
    service=attach_reply_suggestion_routes(app,access,persona_for=lambda char:'Persona '+char)
    payload=request(source).model_dump(mode='json')
    with TestClient(app) as client:
        for extra in [{'owner_id':'other'},{'expected_profile_version':True},{'history':['fake']}]:
            assert client.post('/v2/reply-suggestions',json={**payload,**extra}).status_code==422
        assert model.calls==0
        made=client.post('/v2/reply-suggestions',json=payload);assert made.status_code==200,made.text
        batch=made.json();assert len(batch['suggestions'])==3 and model.calls==1
        assert client.post('/v2/reply-suggestions',json=payload).json()==batch and model.calls==1
        service.model_for=lambda:(_ for _ in ()).throw(AssertionError('GET initializes model'))
        assert client.get('/v2/reply-suggestions/'+batch['batch_id']).json()==batch
        assert client.get('/v2/reply-suggestions/'+str(uuid4())).status_code==404
        ExperienceProfileRepository(store).patch(0,{'tone':'CUTE'})
        assert client.get('/v2/reply-suggestions/'+batch['batch_id']).status_code==409
    store.close()


def test_pending_claim_survives_restart_without_model_replay(tmp_path):
    path=tmp_path/'pending.db';store=SQLiteStore(path);source=event(store);model=Model();service=setup(store,model);req=request(source)
    _,digest=service._snapshot(req.context_ref);row,claimed=service.batches.claim(req,digest);assert claimed
    store.close();store=SQLiteStore(path);service=setup(store,model)
    result=service.generate(req);assert result['status']=='PENDING' and result['batch_id']==row['id'] and model.calls==0
    store.close()


@pytest.mark.parametrize('channel',['DIRECT','GROUP'])
def test_suggestions_use_public_projection_not_internal_image_instructions(tmp_path,channel):
    from character_memory.application.incoming_message import normalize_user_fact
    from character_memory.group_store import GroupRepository,GroupEvent
    from character_memory.domain.reply_suggestions import SuggestionRequest
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'media.db');fact=normalize_user_fact('看这张图片',image={'id':'image-one','original_name':'unpublished-secret-filename.png','mime_type':'image/png','size_bytes':10})
    if channel=='DIRECT':
        source=store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content=fact.runtime_content,metadata=fact.metadata))
        ref=FactRef(kind='EVENT',id=source.id);table='events'
    else:
        repo=GroupRepository(store);group=repo.create_group('Images',['rin','momo'],NOW)
        source=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='t',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=NOW,content=fact.runtime_content,metadata=fact.metadata))
        ref=FactRef(kind='CONVERSATION_EVENT',id=source.id,conversation_id=group.id);table='conversation_events'
    model=Model();service=setup(store,model);req=SuggestionRequest(request_id=uuid4(),context_ref=ref,expected_profile_version=0);batch=service.generate(req)
    assert '看这张图片' in model.prompts[0]
    assert 'unpublished-secret-filename' not in model.prompts[0]
    assert '图片本体已随本轮多模态请求提供' not in model.prompts[0]
    assert '未提供图像' in model.prompts[0]
    metadata={**fact.metadata,'display_text':'更正的可见图注'}
    store.conn.execute(f'UPDATE {table} SET metadata_json=? WHERE id=?',(json.dumps(metadata),source.id));store.conn.commit()
    with pytest.raises(SuggestionStale):service.get(batch['batch_id'])
    store.close()


def test_visible_text_tail_change_invalidates_bounded_prompt_batch(tmp_path):
    from character_memory.application.reply_suggestions import SuggestionStale
    store=SQLiteStore(tmp_path/'tail.db');source=event(store,'a'*4100);service=setup(store);batch=service.generate(request(source))
    store.conn.execute('UPDATE events SET content=? WHERE id=?',('a'*4100+' changed visible tail',source.id));store.conn.commit()
    with pytest.raises(SuggestionStale):service.get(batch['batch_id'])
    store.close()

def test_actual_generation_prompt_contains_the_output_schema(tmp_path):
    from character_memory.domain.reply_suggestions import SuggestionOutput
    store=SQLiteStore(tmp_path/'schema-contract.db');model=Model();service=setup(store,model)
    try:
        source=event(store,'我想练习一个开场')
        service.generate(request(source))
        prompt=model.prompts[0]
        assert 'Output JSON Schema:\n' in prompt
        actual=json.loads(prompt.split('Output JSON Schema:\n',1)[1].splitlines()[0])
        assert actual==SuggestionOutput.model_json_schema()
        assert actual['required']==['suggestions'] and actual['additionalProperties'] is False
        assert actual['properties']['suggestions']['minItems']==3
        assert actual['properties']['suggestions']['maxItems']==5
    finally:store.close()
def test_generation_declares_local_user_as_the_draft_author(tmp_path):
    store=SQLiteStore(tmp_path/'draft-author.db');model=Model();service=setup(store,model)
    try:
        service.generate(request(event(store,'你想先练哪一句？')))
        prompt=model.prompts[0]
        assert 'Draft Author JSON:\n' in prompt
        author=json.loads(prompt.split('Draft Author JSON:\n',1)[1].splitlines()[0])
        assert author=={'actor_type':'USER','actor_id':'user','recipient':{'type':'DIRECT','character_id':'rin'}}
        assert 'USER_MESSAGE' in prompt and '不是伙伴对用户的指导' in prompt
    finally:store.close()
def test_draft_preferences_do_not_turn_user_drafts_into_companion_replies():
    from character_memory.domain.experience_profile import render_experience_profile
    profile={'version':1,'preferences':{'tone':'CUTE','energy':'QUIET','verbosity':'BRIEF'}}
    companion=render_experience_profile(profile)
    draft=render_experience_profile(profile,user_draft=True)
    assert '仅用于面向用户的表达' in companion
    assert '草稿作者始终是用户' in draft
    assert '仅用于面向用户的表达' not in draft
    assert '保留人物自己的兴趣' not in draft