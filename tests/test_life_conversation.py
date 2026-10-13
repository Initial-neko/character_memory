from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event, EventType
from character_memory.life_conversation import LifeReference, resolve_life_reference


def test_reference_is_readonly_owned_and_timed(tmp_path):
    s=SQLiteStore(tmp_path/'test.db')
    e=s.append_event(Event(character_id='a', event_type=EventType.USER_MESSAGE, event_time=datetime.now(timezone.utc), content='原始事实', metadata={'conversation_id':'conv'}))
    ref=LifeReference(character_id='a', key=f'events:{e.id}')
    before=s.conn.total_changes
    data=resolve_life_reference(s,ref)
    assert data['conversation']=={'type':'DIRECT','id':'conv'}
    assert '原始事实' in data['quote'] and '不表示用户亲历' in data['quote']
    assert s.conn.total_changes==before
    with pytest.raises(HTTPException): resolve_life_reference(s,LifeReference(character_id='b',key=ref.key))
    with pytest.raises(HTTPException): resolve_life_reference(s,LifeReference(character_id='a',key='intent:1'))
    s.conn.execute("UPDATE events SET event_time='invalid' WHERE id=?",[e.id])
    with pytest.raises(HTTPException): resolve_life_reference(s,ref)
    with pytest.raises(HTTPException): resolve_life_reference(s,LifeReference(character_id='a',key='events:'+('9'*140)))
    s.close()


def test_group_participation_archive_delete_and_target(tmp_path):
    from character_memory.group_store import GroupRepository, GroupEvent
    from character_memory.life_conversation import prepare_life_message
    s=SQLiteStore(tmp_path/'group.db');repo=GroupRepository(s);now=datetime.now(timezone.utc)
    group=repo.create_group('真实群聊',['a','b'],now)
    e=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='turn',actor_type='CHARACTER',actor_id='a',event_type='CHARACTER_MESSAGE',event_time=now,content='已发生的交流'))
    ref=LifeReference(character_id='a',key=f'group:{e.id}')
    before=s.conn.total_changes
    assert resolve_life_reference(s,ref)['conversation']=={'type':'GROUP','id':group.id}
    assert s.conn.total_changes==before
    with pytest.raises(HTTPException): resolve_life_reference(s,LifeReference(character_id='b',key=ref.key))
    with pytest.raises(HTTPException): prepare_life_message(s,ref,'聊聊',conversation_id='other')
    repo.archive_group(group.id,now)
    with pytest.raises(HTTPException,match='409'): resolve_life_reference(s,ref)
    repo.restore_group(group.id)
    s.conn.execute("DELETE FROM conversation_members WHERE conversation_id=? AND actor_id='user'",[group.id])
    with pytest.raises(HTTPException): resolve_life_reference(s,ref)
    s.conn.execute('DELETE FROM conversation_events WHERE id=?',[e.id])
    with pytest.raises(HTTPException,match='404'): resolve_life_reference(s,ref)
    s.close()


@pytest.fixture
def isolated_app(tmp_path):
    from pathlib import Path
    from character_memory.api import create_api
    from character_memory.group_web import attach_group_routes
    from character_memory.async_web import attach_async_routes
    from character_memory.character_life_web import attach_character_life_routes
    root=Path(__file__).parents[1]
    config=tmp_path/'config.yaml'
    config.write_text(f'api_key: ""\nembedding_provider: deterministic\ndb_path: "{(tmp_path/"isolated.db").as_posix()}"\npersona_path: "{(root/"personas/rin/persona.yaml").as_posix()}"\n',encoding='utf-8')
    app=create_api(str(config));attach_group_routes(app,str(config));attach_async_routes(app)
    attach_character_life_routes(app,app.state.character_memory)
    from character_memory.history_web import attach_history_routes
    attach_history_routes(app)
    scheduler=app.state.character_memory.reaction_scheduler
    scheduler.quiet_seconds=60;scheduler.max_burst_seconds=60
    yield app
    scheduler.close();app.state.character_memory.stream_hub.close()


def test_http_reference_validates_before_writes_and_model(isolated_app):
    from fastapi.testclient import TestClient
    app=isolated_app
    with TestClient(app) as c:
        profiles=c.get('/v1/characters').json()['characters'];character=profiles[0]['id']
        source=c.post('/v1/chat/messages',json={'character_id':character,'conversation_id':'existing','message':'真实原记录'})
        key=f"events:{source.json()['event_id']}"
        ref={'character_id':character,'key':key}
        s=app.state.character_memory.store();before=s.conn.total_changes
        draft=c.get(f'/v1/characters/{character}/life/conversation',params={'key':key})
        assert draft.status_code==200 and s.conn.total_changes==before
        assert c.get(f'/v1/characters/{character}/life/conversation',params={'key':''}).status_code==422
        request={'character_id':character,'conversation_id':'existing','message':'想了解','life_reference':ref}
        assert c.post('/v1/chat',json=request).status_code==422
        bad=c.post('/v1/chat/messages',json={**request,'character_id':profiles[1]['id']})
        assert bad.status_code in (404,409) and s.conn.total_changes==before
        assert c.post('/v1/chat/messages',json={**request,'life_reference':{**ref,'content':'伪造'}}).status_code==422
        sent=c.post('/v1/chat/messages',json=request)
        assert sent.status_code==202,sent.text
        persisted=s.get_event(sent.json()['event_id'])
        assert persisted.metadata['life_reference']==ref and '真实原记录' in persisted.content
        assert c.get('/health').json()['runtime_loaded'] is False
        s.conn.execute('DELETE FROM events WHERE id=?',[source.json()['event_id']]);before=s.conn.total_changes
        assert c.post('/v1/chat/messages',json=request).status_code==404
        assert s.conn.total_changes==before


def test_group_http_confirmed_reference_and_archived_character(isolated_app):
    from fastapi.testclient import TestClient
    from character_memory.group_store import GroupRepository, GroupEvent
    app=isolated_app;access=app.state.character_memory;s=access.store();profiles=access.character_profiles();a,b=[p['id'] for p in profiles[:2]]
    repo=GroupRepository(s);now=datetime.now(timezone.utc);group=repo.create_group('共同经历',[a,b],now)
    source=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='source',actor_type='CHARACTER',actor_id=a,event_type='CHARACTER_MESSAGE',event_time=now,content='人物确实说过'))
    ref={'character_id':a,'key':f'group:{source.id}'}
    with TestClient(app) as c:
        payload={'message':'继续聊聊','life_reference':ref}
        response=c.post(f'/v1/groups/{group.id}/messages',json=payload)
        assert response.status_code==202,response.text
        last=repo.list_events(group.id)[-1]
        assert last.metadata['life_reference']==ref and '人物确实说过' in last.content
        before=s.conn.total_changes
        assert c.post('/v1/chat/messages',json={'character_id':a,'conversation_id':group.id,**payload}).status_code==409
        assert c.post(f'/v1/groups/{group.id}/chat',json=payload).status_code==422
        original=access.character_profiles
        access.character_profiles=lambda:[{**p,'archived_at':'2026-10-14'} if p['id']==a else p for p in profiles]
        try:
            assert c.post(f'/v1/groups/{group.id}/messages',json=payload).status_code==409
        finally: access.character_profiles=original
        assert s.conn.total_changes==before
        assert c.get('/health').json()['runtime_loaded'] is False


@pytest.mark.parametrize('field',['recent','history'])
def test_group_peer_reference_uses_actual_input_receipt(tmp_path,field):
    from datetime import timedelta
    from character_memory.group_store import GroupRepository, GroupEvent
    store=SQLiteStore(tmp_path/'receipt.db');repo=GroupRepository(store)
    now=datetime.now(timezone.utc)-timedelta(minutes=1)
    group=repo.create_group('真实群聊',['b','a'],now-timedelta(minutes=1))
    root=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='real-turn',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=now,content='先聊聊'))
    peer=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='real-turn',actor_type='CHARACTER',actor_id='b',event_type='CHARACTER_MESSAGE',event_time=now+timedelta(seconds=1),content='星灯已经点亮了'))
    receipt={'recent_group_event_ids':[peer.id]} if field=='recent' else {'shared_history_retrieval':{'events':[{'conversation_id':group.id,'source_conversation_event_id':peer.id}]}}
    repo.add_trace(group.id,'real-turn','a',root.id,now+timedelta(seconds=2),receipt)
    before=store.conn.total_changes
    data=resolve_life_reference(store,LifeReference(character_id='a',key=f'group:{peer.id}'))
    assert data['conversation']=={'type':'GROUP','id':group.id}
    assert '星灯已经点亮了' in data['quote'] and '记录发言者/观察者：b' in data['quote']
    assert store.conn.total_changes==before
    store.close()


@pytest.mark.parametrize('case',['missing','malformed','recent_string','recent_overbound','history_scalar','history_string_id','history_wrong_group','history_overbound','wrong_group','wrong_character','future_trace','trace_before_source','joined_after_source'])
def test_group_receipt_cannot_infer_or_cross_visibility(tmp_path,case):
    import json
    from datetime import timedelta
    from character_memory.group_store import GroupRepository, GroupEvent
    from character_memory.time_utils import epoch_us
    store=SQLiteStore(tmp_path/'negative.db');repo=GroupRepository(store);now=datetime.now(timezone.utc)-timedelta(minutes=1)
    group=repo.create_group('来源群',['b','a'],now-timedelta(minutes=1));other=repo.create_group('另一群',['b','a'],now-timedelta(minutes=1))
    root=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='root',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=now,content='输入'))
    peer=repo.append_event(GroupEvent(conversation_id=group.id,turn_id='root',actor_type='CHARACTER',actor_id='b',event_type='CHARACTER_MESSAGE',event_time=now+timedelta(seconds=1),content='真实peer正文'))
    proof={'recent_group_event_ids':[peer.id]};trace_group=group.id;trace_character='a';trace_time=now+timedelta(seconds=2)
    if case in ('missing','malformed'):proof={}
    elif case=='recent_string':proof={'recent_group_event_ids':[str(peer.id)]}
    elif case=='recent_overbound':proof={'recent_group_event_ids':[0]*14+[peer.id]}
    elif case=='history_scalar':proof={'shared_history_retrieval':{'events':['invalid-json']}}
    elif case.startswith('history_'):
        hit={'conversation_id':other.id if case=='history_wrong_group' else group.id,'source_conversation_event_id':str(peer.id) if case=='history_string_id' else peer.id}
        proof={'shared_history_retrieval':{'events':[{}]*4+[hit] if case=='history_overbound' else [hit]}}
    elif case=='wrong_group':trace_group=other.id
    elif case=='wrong_character':trace_character='b'
    elif case=='future_trace':trace_time=datetime.now(timezone.utc)+timedelta(hours=1)
    elif case=='trace_before_source':trace_time=now-timedelta(seconds=1)
    repo.add_trace(trace_group,'root',trace_character,root.id,trace_time,proof)
    with store.transaction():
        if case=='malformed':store.conn.execute("UPDATE conversation_runtime_traces SET trace_json='invalid-json'")
        if case=='joined_after_source':store.conn.execute("UPDATE conversation_members SET joined_at=?,joined_at_epoch=? WHERE conversation_id=? AND actor_id='a'",[(now+timedelta(seconds=3)).isoformat(),epoch_us(now+timedelta(seconds=3)),group.id])
    before=store.conn.total_changes
    with pytest.raises(HTTPException,match='404'):resolve_life_reference(store,LifeReference(character_id='a',key=f'group:{peer.id}'))
    assert store.conn.total_changes==before
    store.close()


def test_real_group_peer_seen_by_follower_can_read_and_send_reference(isolated_app):
    import json
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from character_memory.application.clock import FixedClock
    from character_memory.application.group_conversation_service import GroupConversationService
    from character_memory.domain.models import ActionDecision, ActionType, PersonReaction, DailyLifePlan, DiaryResult
    from character_memory.llm.client import PersonModel
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.runtime.person_runtime import PersonRuntime
    class Model(PersonModel):
        def __init__(self):self.contexts=[]
        def react(self,context):
            self.contexts.append(context)
            return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE,message='星灯已经点亮了' if len(self.contexts)==1 else '我看见你点亮了星灯')])
        def plan_day(self,context):return DailyLifePlan()
        def write_diary(self,context):return DiaryResult(diary='',mental_state_update='')
    app=isolated_app;access=app.state.character_memory;store=access.store();profiles=access.character_profiles();a,b=[p['id'] for p in profiles[:2]]
    model=Model();emb=DeterministicEmbedding();now=datetime.now(timezone.utc)-timedelta(seconds=1)
    runtimes={cid:PersonRuntime(store,VectorRecall(store,emb),emb,model,f'id: {cid}') for cid in (a,b)}
    service=GroupConversationService(store,runtimes,FixedClock(now),profiles=[{'id':a,'name':'A'},{'id':b,'name':'B'}])
    group=service.create_group('真实跟随反应',[b,a]);root=service.persist_user_event(group.id,'聊聊刚才的星灯')
    service.react_from_event(root)
    peer=next(e for e in service.repo.list_events(group.id) if e.actor_id==b)
    assert len(model.contexts)==2 and '星灯已经点亮了' in model.contexts[1]
    trace=store.conn.execute('SELECT source_conversation_event_id,trace_json FROM conversation_runtime_traces WHERE conversation_id=? AND character_id=?',[group.id,a]).fetchone()
    assert trace['source_conversation_event_id']==root.id and root.id!=peer.id
    proof=json.loads(trace['trace_json'])
    if 'recent_group_event_ids' not in proof:pytest.skip('真实输入回执由Draft #282提供；单#283对旧trace保守拒绝，不猜参与')
    assert peer.id in proof['recent_group_event_ids']
    reference={'character_id':a,'key':f'group:{peer.id}'}
    with TestClient(app) as client:
        before=store.conn.total_changes
        response=client.get(f'/v1/characters/{a}/life/conversation',params={'key':reference['key']})
        assert response.status_code==200,response.text
        assert '星灯已经点亮了' in response.json()['quote'] and store.conn.total_changes==before
        sent=client.post(f'/v1/groups/{group.id}/messages',json={'message':'继续聊这条来源','life_reference':reference})
        assert sent.status_code==202,sent.text
        message=service.repo.list_events(group.id)[-1]
        assert message.metadata['life_reference']==reference and '星灯已经点亮了' in message.content
        assert client.get('/health').json()['runtime_loaded'] is False
    assert len(model.contexts)==2
