"""Actual Core goal APIs with a forbidden model and isolated evidence facts."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,os,subprocess
import e2e_realtime_app as fixture
from character_memory.domain.models import Event,EventType
from character_memory.group_store import GroupRepository,GroupEvent

app=fixture.app
ROOT=Path(__file__).resolve().parents[1]
calls=[]
def forbidden(*args,**kwargs):
    calls.append('forbidden');raise AssertionError('Goal API called/initialized model')
fixture.model.react=fixture.model.react_for_session=fixture.model.structured_for_session=forbidden
app.state.character_memory.require_bundle=forbidden
now=datetime.now(timezone.utc)
row=fixture.store.conn.execute("SELECT id FROM events WHERE character_id='rin' ORDER BY id LIMIT 1").fetchone()
if row is None:
    direct=fixture.store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=now,content='Private fixture direct acceptance source'))
    other=fixture.store.append_event(Event(character_id='momo',event_type=EventType.USER_MESSAGE,event_time=now,content='Other character private source'))
    groups=GroupRepository(fixture.store);group=groups.create_group('Goal witness fixture',['rin','momo'],now)
    shared=groups.append_event(GroupEvent(conversation_id=group.id,turn_id='goal-fixture',actor_type='USER',actor_id='user',event_type='USER_MESSAGE',event_time=now,content='Private shared acceptance source'))
    groups.add_trace(group.id,shared.turn_id,'rin',shared.id,now,{})
else:
    direct=fixture.store.get_event(row[0]);other=fixture.store.list_events('momo')[0]
    shared_row=fixture.store.conn.execute("SELECT id,conversation_id FROM conversation_events WHERE turn_id='goal-fixture'").fetchone()
    shared=type('Reference',(),{'id':shared_row['id'],'conversation_id':shared_row['conversation_id']})()

@app.get('/__e2e/goal-runtime')
def audit():
    paths=['src/character_memory/domain/goals.py','src/character_memory/application/goal_evidence.py','src/character_memory/storage/person_goals.py','src/character_memory/goal_web.py','src/character_memory/api.py']
    return {'pid':os.getpid(),'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in paths},'model_calls':len(calls),'external_calls':0,
        'sources':{'direct':{'kind':'EVENT','id':direct.id},'other':{'kind':'EVENT','id':other.id},'shared':{'kind':'CONVERSATION_EVENT','id':shared.id,'conversation_id':shared.conversation_id}},
        'source_event_count':fixture.store.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]}
