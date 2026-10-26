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

def test_goal_read_is_one_sqlite_snapshot_across_independent_writer(tmp_path):
    from character_memory.storage.person_goals import GoalRepository
    from test_person_goals import create_request,source,complete
    stores=[SQLiteStore(tmp_path/'snapshot.sqlite') for _ in range(2)]
    reader,writer=[GoalRepository(s) for s in stores]
    goal=reader.create('rin',create_request())['goal'];ref=source(stores[0]);fired=[]
    def interleave(sql):
        if 'SELECT * FROM person_goal_stages' in sql and not fired:
            fired.append(True);writer.act('rin',goal['id'],complete(1,[ref]))
    stores[0].conn.set_trace_callback(interleave)
    try:
        observed=reader.get('rin',goal['id'])
        assert fired and writer.get('rin',goal['id'])['version']==2
        assert observed['version']==1 and observed['current_stage']==0 and observed['completions']==[]
    finally:
        stores[0].conn.set_trace_callback(None)
        for s in stores:s.close()


def test_invalid_raw_fact_metadata_cannot_become_real_goal_evidence(tmp_path):
    import pytest
    from character_memory.storage.person_goals import GoalRepository,EvidenceUnavailable
    from test_person_goals import create_request,source,complete
    store=SQLiteStore(tmp_path/'metadata.sqlite');repo=GoalRepository(store)
    try:
        ref=source(store);goal=repo.create('rin',create_request())['goal']
        store.conn.execute("UPDATE events SET metadata_json='not-json' WHERE id=?",(ref.id,));store.conn.commit()
        with pytest.raises(EvidenceUnavailable):repo.act('rin',goal['id'],complete(1,[ref]))
        assert repo.get('rin',goal['id'])['version']==1
    finally:store.close()


def test_goal_get_does_not_commit_or_rollback_callers_raw_transaction(tmp_path):
    from character_memory.storage.person_goals import GoalRepository
    from test_person_goals import create_request
    store=SQLiteStore(tmp_path/'rawtx.sqlite');repo=GoalRepository(store)
    try:
        goal=repo.create('rin',create_request())['goal']
        store.conn.execute("UPDATE person_goals SET title='Uncommitted caller value' WHERE id=?",(goal['id'],))
        assert store.conn.in_transaction
        assert repo.get('rin',goal['id'])['title']=='Uncommitted caller value'
        assert store.conn.in_transaction
        store.conn.rollback()
        assert repo.get('rin',goal['id'])['title']==goal['title']
    finally:store.close()
