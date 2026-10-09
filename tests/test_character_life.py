from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from character_memory.character_life import CharacterLifeReader
from character_memory.character_life_web import attach_character_life_routes
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event, EventType, Memory
from character_memory.group_store import GroupRepository
from character_memory.space_store import SpaceRepository
from character_memory.world_activity import WorldPulseRepository

NOW=datetime(2026,10,9,1,20,tzinfo=timezone.utc)

@pytest.fixture
def store(tmp_path):
    s=SQLiteStore(tmp_path/'life.db')
    yield s
    s.close()


def event(s, character='a', content='真实交流', at=NOW):
    return s.append_event(Event(character_id=character,event_type=EventType.USER_MESSAGE,event_time=at,content=content))


def test_source_state_memory_links_and_no_writes(store):
    e=event(store)
    store.set_mental_state('a','之前的状态',NOW-timedelta(hours=1))
    store.set_mental_state('a','新的状态',NOW,e.id)
    store.add_memory(Memory(character_id='a',content='共同经历',event_time=NOW,source_event_id=e.id,embedding=[1.0]))
    event(store,'b','私人秘密')
    store.add_runtime_trace('a',e.id,NOW,{'reaction':'已输出的决策摘要','actions':[], 'context':'private prompt','raw_model_response':'hidden raw'})
    before=store.conn.total_changes
    data=CharacterLifeReader(store).timeline('a',day='2026-10-09')
    assert store.conn.total_changes==before
    assert len(data['items'])==1
    i=data['items'][0]
    assert i['state_change']=={'before':'之前的状态','after':'新的状态'}
    assert len(i['memories'])==1 and i['source']['event_id']==e.id
    assert i['status']=='NO_ACTION'
    assert 'hidden raw' not in str(data) and 'private prompt' not in str(data) and '私人秘密' not in str(data)


def test_same_timestamp_cursor_no_duplicates(store):
    for n in range(20): event(store,content=f'记录{n}')
    reader=CharacterLifeReader(store);seen=[];cursor=''
    while True:
        page=reader.timeline('a',limit=3,cursor=cursor)
        seen += [i['key'] for i in page['items']]
        cursor=page['next_cursor']
        if not cursor:break
    assert len(seen)==len(set(seen))==20


def test_day_query_and_unknown_history(store):
    event(store,at=datetime(2026,10,8,15,59,tzinfo=timezone.utc))
    event(store,content='100%_配色',at=datetime(2026,10,8,16,0,tzinfo=timezone.utc))
    reader=CharacterLifeReader(store)
    assert len(reader.timeline('a',day='2026-10-09',query='100%_')['items'])==1
    assert reader.timeline('b')['items']==[]
    assert reader.timeline('a',day='2026-10-09',channel='WORLD')['items']==[]
    with pytest.raises(ValueError):reader.timeline('a',cursor='garbage')
    with pytest.raises(ValueError):reader.timeline('a',day='bad')


def test_optional_tables_world_actual_no_action_not_idle(store):
    WorldPulseRepository(store)
    store.conn.execute("INSERT INTO world_browse_decisions(opportunity_id,character_id,started_at,phase,plan_json,result_json) VALUES(?,?,?,?,?,?)",['one','a',NOW.isoformat(),'APPLIED','{"decision":{"choice":"NO_ACTION"}}','{"execution_status":"SKIPPED"}'])
    reader=CharacterLifeReader(store)
    page=reader.timeline('a',channel='WORLD')
    assert len(page['items'])==1 and '未行动' in page['items'][0]['title']
    assert page['items'][0]['decision']['basis'] is None
    assert reader.timeline('a',day='2026-10-10')['items']==[]


def test_intent_is_plan_not_done_activity(store):
    store.add_intent('a','继续讨论','PROACTIVE_MESSAGE',NOW,NOW+timedelta(hours=3),NOW+timedelta(days=2))
    reader=CharacterLifeReader(store)
    assert reader.timeline('a')['items']==[]
    item=reader.timeline('a',view='intents')['items'][0]
    assert item['status']=='PENDING' and item['intent']['earliest_at']


def test_routes_readonly_unknown_validation(store):
    app=FastAPI()
    def ensure(cid):
        if cid!='a':raise HTTPException(404,'unknown character')
    attach_character_life_routes(app,SimpleNamespace(read_store=store,ensure_character=ensure,web_dir=Path(__file__).parents[1]/'src/character_memory/web'))
    c=TestClient(app)
    assert c.get('/life').status_code==200
    assert c.get('/v1/characters/a/life').json()['read_only']
    assert c.get('/v1/characters/missing/life').status_code==404
    assert c.get('/v1/characters/a/life?limit=900').status_code==422
    assert c.get('/v1/characters/a/life?cursor=bad').status_code==422
    assert c.post('/v1/characters/a/life').status_code==405


def test_group_actual_participation_namespace_state_and_peer(store):
    import json
    from character_memory.group_store import GroupEvent
    g=GroupRepository(store);group=g.create_group('讨论',['a','b','c'],NOW)
    msg=g.append_event(GroupEvent(conversation_id=group.id,turn_id='one',actor_type='CHARACTER',actor_id='b',event_type='CHARACTER_MESSAGE',event_time=NOW,content='一起讨论'))
    g.add_trace(group.id,'one','a',msg.id,NOW,{'mental_state_before':'旧状态','mental_state_after':'新状态','actions':[], 'created_memory_ids':[]})
    event(store,'a','Direct 不能误关联群聊记忆')
    store.add_memory(Memory(character_id='a',content='群聊来源',event_time=NOW,source_event_id=None,embedding=[1],metadata={'origin':'GROUP','conversation_id':group.id,'source_conversation_event_id':msg.id}))
    reader=CharacterLifeReader(store)
    i=reader.timeline('a',channel='GROUP')['items'][0]
    assert i['state_change']=={'before':'旧状态','after':'新状态'}
    assert len(i['memories'])==1 and i['source']['event_id'] is None
    assert reader.timeline('c',channel='GROUP')['items']==[]
    assert len(reader.timeline('a',view='changes',channel='GROUP')['items'])==1
    assert len(reader.timeline('a',view='relationships',peer='b')['items'])==1
    assert reader.timeline('a',view='relationships',peer='c')['items']==[]


def test_space_actual_comment_and_read_only(store):
    repo=SpaceRepository(store)
    post=repo.create_post('b','公开话题',NOW)
    repo.add_comment(post.id,'a','我的看法',NOW)
    reader=CharacterLifeReader(store)
    assert len(reader.timeline('a',channel='SPACE')['items'])==1
    assert reader.timeline('c',channel='SPACE')['items']==[]
    i=reader.timeline('a',view='relationships',peer='b')['items'][0]
    assert i['participants']==[{'id':'b','name':'b'}]
    assert reader.timeline('a',view='relationships',peer='c')['items']==[]


def test_space_opportunity_no_post_is_real_and_unknown_reason(store):
    SpaceRepository(store)
    from character_memory.time_utils import epoch_us
    store.conn.execute("INSERT INTO space_opportunity_runs(character_id,scheduled_for,scheduled_for_epoch,started_at,started_at_epoch,status) VALUES(?,?,?,?,?,?)",['a',NOW.isoformat(),epoch_us(NOW),NOW.isoformat(),epoch_us(NOW),'NO_POST'])
    item=CharacterLifeReader(store).timeline('a',channel='SPACE')['items'][0]
    assert item['status']=='NO_POST' and item['decision']['basis'] is None
    assert CharacterLifeReader(store).timeline('b',channel='SPACE')['items']==[]


def test_world_search_never_searches_hidden_plan_persona(store):
    WorldPulseRepository(store)
    store.conn.execute("INSERT INTO world_browse_decisions(opportunity_id,character_id,started_at,phase,plan_json,result_json) VALUES(?,?,?,?,?,?)",['one','a',NOW.isoformat(),'APPLIED','{"persona":"secret-canary","decision":{"choice":"NO_ACTION"}}','{"execution_status":"SKIPPED"}'])
    assert CharacterLifeReader(store).timeline('a',query='secret-canary')['items']==[]


def test_due_expiry_and_actual_deferral_history(store):
    now=datetime.now(timezone.utc)
    ident=store.add_intent('a','继续交流','PROACTIVE_MESSAGE',now-timedelta(hours=1),now-timedelta(minutes=1),now+timedelta(days=1))
    reader=CharacterLifeReader(store)
    assert reader.timeline('a',view='intents')['items'][0]['intent']['due']
    store.set_intent_status(ident,'PROCESSING')
    store.defer_intent(ident,now=now,defer_hours=3)
    plan=reader.timeline('a',view='intents')['items'][0]
    assert plan['intent']['defer_count']==1 and not plan['intent']['due']
    audit=reader.timeline('a')['items'][0]
    assert audit['kind']=='intent_audit' and audit['status']=='DEFERRED' and audit['time']
    assert reader.timeline('b')['items']==[]
