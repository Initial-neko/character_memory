"""Real SQLite/Person boundary checks; simulated facts remain auditable."""
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
import pytest
from test_reaction_receipts import NOW, CountedModel, runtime, group_service, snapshot
from character_memory.domain.models import Event, EventType, Memory
from character_memory.storage.sqlite import SQLiteStore

SIM={'schema_version':1,'reality':'SIMULATED','activity_id':'practice','branch_id':'other'}
REAL={'schema_version':1,'reality':'REAL'}
BAD=[None,{},'REAL',True,{'schema_version':True,'reality':'REAL'},{'schema_version':2,'reality':'REAL'},{**REAL,'branch_id':'ambiguous'},SIM]

@pytest.mark.parametrize('scope',BAD)
def test_direct_denies_nonreal_before_any_write_or_model(tmp_path,scope):
    store=SQLiteStore(tmp_path/'boundary.db');model=CountedModel();person=runtime(store,model)
    before=snapshot(store)
    event=Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='simulated trigger',metadata={'experience_scope':scope})
    with pytest.raises(ValueError,match='experience scope'):person.handle(event)
    assert model.calls==0 and snapshot(store)==before
    store.close()

def test_authoritative_scope_cannot_be_stripped_by_caller(tmp_path):
    store=SQLiteStore(tmp_path/'authority.db');model=CountedModel();person=runtime(store,model)
    event=store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='simulated trigger',metadata={'experience_scope':SIM}))
    before=snapshot(store)
    with pytest.raises(ValueError,match='experience scope'):person.handle(event.model_copy(update={'metadata':{}}),persist_event=False)
    assert model.calls==0 and snapshot(store)==before
    store.close()

@pytest.mark.parametrize('scope',[None,REAL])
def test_existing_real_behavior_is_preserved(tmp_path,scope):
    store=SQLiteStore(tmp_path/'real.db');model=CountedModel();person=runtime(store,model)
    meta={} if scope is None else {'experience_scope':scope}
    person.handle(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='real trigger',metadata=meta))
    assert model.calls==1 and store.list_memories('rin')
    store.close()

def test_real_context_filters_before_limits_and_keeps_audit_after_reopen(tmp_path):
    path=tmp_path/'contexts.db';store=SQLiteStore(path);emb=runtime(store,CountedModel()).embeddings
    for i in range(12):
        store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW+timedelta(seconds=i),content=f'real-{i}'))
    store.add_memory(Memory(character_id='rin',content='real memory',event_time=NOW,importance=.5,embedding=emb.embed('practice')))
    for i in range(25):
        metadata={'experience_scope':SIM}
        store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW+timedelta(seconds=20+i),content='SIMULATION_SECRET',metadata=metadata))
        store.append_event(Event(character_id='rin',event_type=EventType.WORLD_OBSERVATION,event_time=NOW+timedelta(seconds=20+i),content='practice SIMULATION_SECRET',metadata=metadata))
        store.add_memory(Memory(character_id='rin',content='SIMULATION_SECRET',event_time=NOW+timedelta(seconds=i),importance=1,pinned=True,metadata=metadata,embedding=emb.embed('practice')))
    store.close();store=SQLiteStore(path)
    before=store.conn.total_changes
    person=runtime(store,CountedModel());ctx=person.context_builder.build('rin',query='practice',at=NOW+timedelta(minutes=2))
    assert [e.content for e in ctx.recent_events]==[f'real-{i}' for i in range(4,12)]
    assert [m.content for m in ctx.memories]==['real memory']
    assert not ctx.observed_events
    assert len(store.list_events('rin',limit=100))==62
    assert len(store.list_memories('rin'))==26 and store.conn.total_changes==before
    store.close()

def test_group_denies_forged_source_and_filters_shared_history(tmp_path):
    store=SQLiteStore(tmp_path/'group.db');models={'rin':CountedModel(),'momo':CountedModel()};service=group_service(store,models)
    group=service.create_group('practice',['rin','momo'],at=NOW)
    real=service.persist_user_event(group.id,'real practice',at=NOW)
    event=service.persist_user_event(group.id,'SIMULATION_SECRET practice',at=NOW+timedelta(seconds=1))
    store.conn.execute('UPDATE conversation_events SET metadata_json=? WHERE id=?',(json.dumps({'experience_scope':SIM}),event.id));store.conn.commit()
    with pytest.raises(ValueError,match='experience scope'):service.react_from_event(event)
    assert sum(m.calls for m in models.values())==0
    recent=service._recent_as_events(group.id)
    assert [e.metadata['group_event_id'] for e in recent]==[real.id]
    recalled=service.repo.recall_shared_events(group.id,'rin','practice',at=NOW+timedelta(minutes=1),max_id=event.id)
    assert [e.id for e in recalled]==[real.id]
    assert len(service.repo.list_events(group.id))==2
    store.close()

@pytest.mark.parametrize('scope',[*BAD,REAL,'absent'])
def test_python_sql_scope_parity(scope):
    from character_memory.domain.experience_scope import is_real_experience, real_experience_sql
    metadata={} if scope=='absent' else {'experience_scope':scope}
    conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE records(metadata_json TEXT)');conn.execute('INSERT INTO records VALUES (?)',(json.dumps(metadata),))
    actual=conn.execute('SELECT '+real_experience_sql('metadata_json')+' FROM records').fetchone()[0]
    assert bool(actual)==is_real_experience(metadata)==(scope=='absent' or scope is REAL)
    conn.close()


def test_late_scope_change_prevents_real_derived_commit(tmp_path):
    store=SQLiteStore(tmp_path/'late.db')
    event=store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='real initially'))
    class ChangingModel(CountedModel):
        def react_call_for_session(self,context,session_id):
            result=super().react_call_for_session(context,session_id)
            store.conn.execute('UPDATE events SET metadata_json=? WHERE id=?',(json.dumps({'experience_scope':SIM}),event.id));store.conn.commit()
            return result
    model=ChangingModel();person=runtime(store,model)
    with pytest.raises(ValueError,match='experience scope'):person.handle(event,persist_event=False)
    assert model.calls==1 and not store.list_memories('rin')
    assert store.conn.execute('SELECT COUNT(*) FROM mental_state_history').fetchone()[0]==0
    assert store.get_runtime_trace(event.id) is None
    store.close()


def test_simulated_exact_memory_does_not_suppress_real_admission(tmp_path):
    store=SQLiteStore(tmp_path/'dedup.db');model=CountedModel();person=runtime(store,model)
    store.add_memory(Memory(character_id='rin',content='一次共同经历',event_time=NOW,importance=1,embedding=person.embeddings.embed('一次共同经历'),metadata={'experience_scope':SIM}))
    person.handle(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='real'))
    rows=store.list_memories('rin')
    assert len(rows)==2 and sum('experience_scope' not in m.metadata for m in rows)==1
    store.close()

@pytest.mark.parametrize('channel',['DIRECT','WORLD','SPACE'])
def test_shared_channel_context_never_includes_supplied_simulation(tmp_path,channel):
    from character_memory.runtime.context import compile_context
    store=SQLiteStore(tmp_path/'supplied.db');person=runtime(store,CountedModel())
    events=[Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='SIMULATION_SECRET',metadata={'experience_scope':SIM})]
    ctx=person.context_builder.build('rin',query='practice',at=NOW,recent_events=events)
    if channel=='WORLD':
        from character_memory.world_continuation import WorldContinuation
        service=WorldContinuation.__new__(WorldContinuation);service.service=SimpleNamespace(_memory_text=lambda c:'none')
        prompt=service._prompt(ctx,[],[],[],1)
    else:prompt=compile_context('persona',ctx.mental_state,ctx.memories,Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='practice'),recent_events=ctx.recent_events,observed_events=ctx.observed_events)
    assert 'SIMULATION_SECRET' not in prompt
    store.close()


def test_proactive_sharing_cannot_strip_simulated_scope(tmp_path):
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.runtime.context import compile_context
    store=SQLiteStore(tmp_path/'sharing.db')
    store.append_event(Event(character_id='rin',event_type=EventType.WORLD_OBSERVATION,event_time=NOW,content='SIMULATION_SECRET',metadata={'channel':'PERSONAL_BROWSE','sources':['https://example.test'], 'experience_scope':SIM}))
    candidates=ChatService(store,None,FixedClock(NOW))._sharing_observations('rin',NOW)
    assert candidates==[]
    store.close()

@pytest.mark.parametrize('kind',['direct','space','group'])
def test_visual_planning_recent_dialogue_has_no_simulation(tmp_path,kind):
    from character_memory.visual_runtime import DirectVisualRuntime
    from character_memory.space_media_executor import SpaceMediaExecutor
    from character_memory.group_autonomous_visual import _group_recent_dialogue
    from character_memory.group_store import GroupRepository,GroupEvent
    store=SQLiteStore(tmp_path/'visual.db')
    store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW,content='SIMULATION_SECRET',metadata={'experience_scope':SIM}))
    if kind=='group':
        repo=GroupRepository(store);group=repo.create_group('practice',['rin','momo'],NOW)
        repo.append_event(GroupEvent(conversation_id=group.id,turn_id='sim',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=NOW,content='SIMULATION_SECRET',metadata={'experience_scope':SIM}))
        lines=_group_recent_dialogue(store,group.id)
    else:lines=(DirectVisualRuntime if kind=='direct' else SpaceMediaExecutor)._recent_dialogue(SimpleNamespace(store=store),'rin',NOW)
    assert 'SIMULATION_SECRET' not in str(lines)
    store.close()

def test_role_contact_does_not_turn_simulation_into_real_reply(tmp_path):
    from character_memory.group_store import GroupRepository,GroupEvent
    from character_memory.application.role_collaboration import CollaborationContext
    from character_memory.application.collaboration_runtime import build_role_collaboration
    store=SQLiteStore(tmp_path/'collaboration.db');repo=GroupRepository(store)
    group=repo.create_group('practice',['alice','bob'],NOW)
    repo.append_event(GroupEvent(conversation_id=group.id,turn_id='sim',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=NOW,content='SIMULATION_SECRET',metadata={'experience_scope':SIM}))
    seen=[]
    class Model:
        def structured_for_session(self,prompt,schema,session):
            seen.append(prompt);return schema(message='real reply',research_query=None)
    bundle=SimpleNamespace(runtimes={'bob':runtime(store,CountedModel())},model=Model())
    access=SimpleNamespace(store=lambda:store,character_profiles=lambda:[{'id':'alice'},{'id':'bob'}],require_bundle=lambda:bundle,services=SimpleNamespace(claude_runner=None),world_observer=None,settings=SimpleNamespace(),space_repository=None)
    result=build_role_collaboration(access).contact('bob','practice',context=CollaborationContext('alice','GROUP',group.id,'op',None,NOW))
    assert result['status']=='COMPLETED' and seen and 'SIMULATION_SECRET' not in seen[0]
    store.close()


def test_simulated_scene_control_does_not_advance_real_projection(tmp_path):
    from test_group_scene import scene
    from character_memory.group_scenes import GroupScenes
    from character_memory.group_store import GroupRepository,GroupEvent
    _,store,_,identity=scene(tmp_path)
    GroupRepository(store).append_event(GroupEvent(conversation_id=identity,turn_id='sim-day',actor_type='SYSTEM',actor_id='system',event_type='SCENE_DAY_ADVANCED',event_time=NOW,content='simulated day',metadata={'experience_scope':SIM,'to_day':99,'from_day':1,'request_id':'same-key'}))
    assert GroupScenes(store).state(identity)['story_day']==1
    assert GroupScenes(store).advance(identity,expected_day=1,request_id='same-key')['story_day']==2
    store.close()
