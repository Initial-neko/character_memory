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
