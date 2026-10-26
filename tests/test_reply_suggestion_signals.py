"""Signals describe accepted facts, never mere draft clicks or guessed preference."""
from uuid import uuid4
from types import SimpleNamespace
import json
import pytest
from test_message_receipts import acceptance,body,endpoint
from test_reply_suggestions import Model,NOW
from character_memory.domain.models import Event,EventType
from character_memory.domain.facts import FactRef
from character_memory.group_store import GroupRepository,GroupEvent
from character_memory.storage.experience_profiles import ExperienceProfileRepository


def batch_for(client,store,group,scope):
    from character_memory.application.reply_suggestion_signals import suggestion_service
    from character_memory.domain.reply_suggestions import SuggestionRequest
    service=suggestion_service(store,client.app.state.character_memory);service.model_for=lambda:Model()
    if scope=='DIRECT':
        source=store.append_event(Event(character_id='momo',event_type=EventType.CHARACTER_MESSAGE,event_time=NOW,content='一起准备聚会吧'))
        ref=FactRef(kind='EVENT',id=source.id)
    else:
        source=GroupRepository(store).append_event(GroupEvent(conversation_id=group.id,turn_id='t',actor_type='CHARACTER',actor_id='momo',event_type='CHARACTER_MESSAGE',event_time=NOW,content='一起准备聚会吧'))
        ref=FactRef(kind='CONVERSATION_EVENT',id=source.id,conversation_id=group.id)
    return service.generate(SuggestionRequest(request_id=uuid4(),context_ref=ref,expected_profile_version=0))


@pytest.mark.parametrize('scope',['DIRECT','GROUP'])
@pytest.mark.parametrize('mode',['SELECT','EDIT','FREE_INPUT'])
def test_real_accepted_fact_and_signal_are_idempotent(acceptance,scope,mode):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,scope);choice=batch['suggestions'][0]
    attribution={'batch_id':batch['batch_id']}
    if mode!='FREE_INPUT':attribution['suggestion_id']=choice['id']
    message=choice['text'] if mode=='SELECT' else '我想用自己的说法再聊聊'
    payload={**body(scope),'message':message,'reply_suggestion':attribution}
    sent=client.post(endpoint(group,scope),json=payload);assert sent.status_code==202,sent.text
    signal=sent.json()['reply_suggestion_signal'];assert signal['kind']==mode and signal['source_ref']['id']==sent.json()['event_id']
    assert signal['source_ref']['kind']==('EVENT' if scope=='DIRECT' else 'CONVERSATION_EVENT')
    retry=client.post(endpoint(group,scope),json=payload);assert retry.status_code==202,retry.text
    assert retry.json()['reply_suggestion_signal']==signal and len(calls)==1
    rows=store.conn.execute('SELECT * FROM reply_suggestion_signals').fetchall();assert len(rows)==1


def test_stale_or_wrong_target_rejected_without_acceptance(acceptance):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    attribution={'batch_id':batch['batch_id'],'suggestion_id':batch['suggestions'][0]['id']}
    bad={**body('DIRECT'),'character_id':'rin','reply_suggestion':attribution}
    assert client.post('/v1/chat/messages',json=bad).status_code==409
    assert len(store.list_chat_events('rin'))==0 and len(calls)==0
    ExperienceProfileRepository(store).patch(0,{'tone':'CUTE'})
    assert client.post('/v1/chat/messages',json={**body('DIRECT'),'reply_suggestion':attribution}).status_code==409
    assert len(calls)==0


def test_signal_failure_rolls_back_source_receipt_and_delivery(acceptance,monkeypatch):
    from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT');before=store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    monkeypatch.setattr(ReplySuggestionSignalRepository,'record',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('signal write failed')))
    payload={**body('DIRECT'),'reply_suggestion':{'batch_id':batch['batch_id']}}
    with pytest.raises(RuntimeError,match='signal write failed'):client.post('/v1/chat/messages',json=payload)
    assert store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==before and len(calls)==0
    assert store.conn.execute('SELECT COUNT(*) FROM message_acceptance_receipts').fetchone()[0]==0


def test_attribution_requires_identity_and_rejects_self_reported_mode(acceptance):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    no_identity={**body('DIRECT'),'reply_suggestion':{'batch_id':batch['batch_id']}};no_identity.pop('request_id')
    assert client.post('/v1/chat/messages',json=no_identity).status_code==422
    assert client.post('/v1/chat/messages',json={**body('DIRECT'),'reply_suggestion':{'batch_id':batch['batch_id'],'kind':'SELECT'}}).status_code==422
    assert len(calls)==0


def test_old_message_fingerprint_omits_new_absent_field(acceptance):
    from character_memory.async_web import AsyncChatRequest
    from character_memory.storage.message_receipts import MessageReceiptRepository
    client,store,group,calls,_=acceptance;payload=body('DIRECT');request=AsyncChatRequest(**payload)
    old_payload=request.model_dump(mode='json',exclude={'request_id','reply_suggestion'})
    source=store.append_event(Event(character_id='momo',event_type=EventType.USER_MESSAGE,event_time=NOW,content='原始消息'))
    MessageReceiptRepository(store).accept(['DIRECT','momo','one'],payload['request_id'],old_payload,lambda:{'accepted':True,'event_id':source.id},source_kind='EVENT')
    result=client.post('/v1/chat/messages',json=payload);assert result.status_code==202,result.text
    assert result.json()['event_id']==source.id and len(calls)==0


def test_ignore_and_explicit_feedback_are_atomic_and_not_model_calls(acceptance):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    ignored={'request_id':str(uuid4()),'batch_id':batch['batch_id'],'kind':'IGNORE'}
    one=client.post('/v2/reply-suggestions/feedback',json=ignored);assert one.status_code==200,one.text
    assert client.post('/v2/reply-suggestions/feedback',json=ignored).json()==one.json()
    assert ExperienceProfileRepository(store).get()['version']==0
    feedback={'request_id':str(uuid4()),'batch_id':batch['batch_id'],'kind':'EXPLICIT_FEEDBACK','comment':'我希望简短一点','expected_profile_version':0,'preference_changes':{'verbosity':'BRIEF'}}
    changed=client.post('/v2/reply-suggestions/feedback',json=feedback);assert changed.status_code==200,changed.text
    assert changed.json()['applied_profile_version']==1
    assert client.post('/v2/reply-suggestions/feedback',json=feedback).json()==changed.json()
    assert ExperienceProfileRepository(store).get()['version']==1 and ExperienceProfileRepository(store).get()['preferences']=={'verbosity':'BRIEF'}
    assert len(calls)==0 and len(store.list_chat_events('momo'))==1


def test_stale_feedback_version_does_not_record_partial_signal(acceptance):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    failed=client.post('/v2/reply-suggestions/feedback',json={'request_id':str(uuid4()),'batch_id':batch['batch_id'],'kind':'EXPLICIT_FEEDBACK','preference_changes':{'tone':'NATURAL'},'expected_profile_version':1})
    assert failed.status_code==409
    assert ExperienceProfileRepository(store).get()['version']==0
    assert store.conn.execute('SELECT COUNT(*) FROM reply_suggestion_signals').fetchone()[0]==0


@pytest.mark.parametrize('scope',['DIRECT','GROUP'])
def test_unknown_batch_is_404_without_writing_source(acceptance,scope):
    client,store,group,calls,_=acceptance
    result=client.post(endpoint(group,scope),json={**body(scope),'reply_suggestion':{'batch_id':str(uuid4())}})
    assert result.status_code==404
    assert len(calls)==0 and store.conn.execute('SELECT COUNT(*) FROM reply_suggestion_signals').fetchone()[0]==0


@pytest.mark.parametrize('invalid',[
    {'kind':'EXPLICIT_FEEDBACK'},
    {'kind':'IGNORE','preference_changes':{'tone':'CUTE'},'expected_profile_version':0},
    {'kind':'EXPLICIT_FEEDBACK','preference_changes':{'tone':'CUTE'}},
    {'kind':'EXPLICIT_FEEDBACK','preference_changes':{'tone':'CUTE'},'expected_profile_version':True},
    {'kind':'EXPLICIT_FEEDBACK','comment':'test','owner_id':'other'},
])
def test_feedback_requires_explicit_valid_changes(acceptance,invalid):
    client,store,group,calls,_=acceptance;batch=batch_for(client,store,group,'DIRECT')
    response=client.post('/v2/reply-suggestions/feedback',json={'request_id':str(uuid4()),'batch_id':batch['batch_id'],**invalid})
    assert response.status_code==422
    assert ExperienceProfileRepository(store).get()['version']==0


def test_concurrent_explicit_feedback_has_one_profile_cas_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.application.reply_suggestion_signals import record_feedback
    from character_memory.application.reply_suggestions import SuggestionStale
    from character_memory.storage.experience_profiles import ProfileConflict
    from character_memory.domain.reply_suggestions import SuggestionFeedback
    from test_reply_suggestions import setup,event,request
    stores=[SQLiteStore(tmp_path/'feedback-race.db'),SQLiteStore(tmp_path/'feedback-race.db')]
    source=event(stores[0]);services=[setup(store) for store in stores];batch=services[0].generate(request(source));gate=threading.Barrier(2)
    def update(index):
        gate.wait()
        try:
            return record_feedback(services[index],SuggestionFeedback(request_id=uuid4(),batch_id=batch['batch_id'],kind='EXPLICIT_FEEDBACK',expected_profile_version=0,preference_changes={'tone':['CUTE','NATURAL'][index]}))['applied_profile_version']
        except (SuggestionStale,ProfileConflict):return 'CONFLICT'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(update,[0,1]))
    assert sorted(map(str,results))==['1','CONFLICT']
    assert stores[0].conn.execute('SELECT COUNT(*) FROM reply_suggestion_signals').fetchone()[0]==1
    for store in stores:store.close()
