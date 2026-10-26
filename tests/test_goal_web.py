from types import SimpleNamespace
from datetime import datetime,timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event,EventType
from character_memory.goal_web import attach_goal_routes


def test_goal_api_lifecycle_is_person_scoped_and_never_initializes_model(tmp_path):
    store=SQLiteStore(tmp_path/'goals.sqlite')
    event=store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),content='Local operator source'))
    app=FastAPI()
    def forbidden():raise AssertionError('Goal metadata API initialized model')
    app.state.character_memory=SimpleNamespace(read_store=store,character_profiles=lambda:[{'id':'rin'}],require_bundle=forbidden)
    attach_goal_routes(app)
    create={'request_id':'create-01','title':'Read together','success_criteria':'Review two readings','stages':['First reading','Second reading']}
    try:
        with TestClient(app) as client:
            base='/v1/characters/rin/goals'
            assert client.post('/v1/characters/missing/goals',json=create).status_code==404
            bad=client.post(base,json={**create,'unknown':'not allowed'});assert bad.status_code==422
            response=client.post(base,json=create);assert response.status_code==201,response.text
            goal=response.json()['goal'];gid=goal['id']
            assert goal['version']==1 and goal['phase']=='ACTIVE'
            assert client.get('/v1/characters/other/goals/'+gid).status_code==404
            action=base+'/'+gid+'/actions'
            pause={'request_id':'pause-001','expected_version':1,'operation':'PAUSE'}
            assert client.post(action,json=pause).json()['goal']['phase']=='PAUSED'
            assert client.post(action,json={**pause,'request_id':'stale-001'}).status_code==409
            resume={'request_id':'resume-01','expected_version':2,'operation':'RESUME'}
            assert client.post(action,json=resume).json()['goal']['version']==3
            complete={'request_id':'stage-001','expected_version':3,'operation':'COMPLETE_STAGE','confirmation':'OPERATOR_ACCEPTED','evidence':[{'kind':'EVENT','id':event.id}]}
            assert client.post(action,json={**complete,'confirmation':None}).status_code==422
            unavailable={**complete,'evidence':[{'kind':'EVENT','id':999999}]}
            assert client.post(action,json=unavailable).status_code==409
            one=client.post(action,json=complete);assert one.status_code==200,one.text
            assert one.json()['goal']['current_stage']==1
            two=client.post(action,json={**complete,'request_id':'stage-002','expected_version':4})
            assert two.json()['goal']['phase']=='COMPLETED'
            archive=client.post(action,json={'request_id':'archive-1','expected_version':5,'operation':'ARCHIVE'})
            assert archive.json()['goal']['phase']=='ARCHIVED'
            replay=client.post(action,json=complete).json()
            assert replay['replayed'] and replay['goal']['phase']=='ARCHIVED'
            before=store.conn.total_changes
            assert client.get(base+'/'+gid).json()['version']==6
            assert len(client.get(base).json()['goals'])==1
            assert store.conn.total_changes==before
            assert len(store.list_events('rin'))==1
    finally:store.close()

def test_optional_operator_note_cannot_be_blank():
    import pytest
    from pydantic import ValidationError
    from character_memory.domain.goals import GoalAction
    for note in ('','   ','\n\t'):
        with pytest.raises(ValidationError):
            GoalAction(request_id='blank-note',expected_version=1,operation='COMPLETE_STAGE',confirmation='OPERATOR_ACCEPTED',evidence=[{'kind':'EVENT','id':1}],note=note)
