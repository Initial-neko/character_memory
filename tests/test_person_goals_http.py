import json,os
from pathlib import Path
from test_model_budget_http import server

def test_goal_lifecycle_over_actual_http_and_restart(tmp_path):
    base='/v1/characters/rin/goals'
    create={'request_id':'create-http','title':'Review a real reading','success_criteria':'Operator accepts two stages','stages':['Private direct source','Witnessed shared source']}
    with server(tmp_path,app_module='e2e_person_goals_app') as client:
        before=client.get('/__e2e/goal-runtime').json();refs=before['sources']
        created=client.post(base,json=create);assert created.status_code==201,created.text
        gid=created.json()['goal']['id'];path=base+'/'+gid;action=path+'/actions'
        assert client.get('/v1/characters/momo/goals/'+gid).status_code==404
        assert client.post(action,json={'request_id':'pause-http','expected_version':1,'operation':'PAUSE'}).json()['goal']['phase']=='PAUSED'
        one={'request_id':'stage-http-1','expected_version':2,'operation':'COMPLETE_STAGE','confirmation':'OPERATOR_ACCEPTED','evidence':[refs['direct']]}
        assert client.post(action,json=one).status_code==409
        assert client.post(action,json={'request_id':'resume-http','expected_version':2,'operation':'RESUME'}).json()['goal']['version']==3
        one['expected_version']=3
        assert client.post(action,json={**one,'evidence':[refs['other']]}).status_code==409
        assert client.post(action,json={**one,'expected_version':True}).status_code==422
        accepted=client.post(action,json=one);assert accepted.status_code==200,accepted.text
        assert accepted.json()['goal']['current_stage']==1
        assert client.post(action,json={**one,'request_id':'stale-http'}).status_code==409
        after_first=client.get('/__e2e/goal-runtime').json()
        assert after_first['model_calls']==0 and after_first['source_event_count']==before['source_event_count']
    with server(tmp_path,app_module='e2e_person_goals_app') as client:
        resumed=client.get(path).json();assert resumed['phase']=='ACTIVE' and resumed['current_stage']==1 and resumed['version']==4
        assert client.post(base,json=create).json()['goal']['id']==gid
        second={'request_id':'stage-http-2','expected_version':4,'operation':'COMPLETE_STAGE','confirmation':'OPERATOR_ACCEPTED','evidence':[refs['shared']]}
        done=client.post(action,json=second);assert done.status_code==200,done.text
        assert done.json()['goal']['phase']=='COMPLETED'
        archived=client.post(action,json={'request_id':'archive-http','expected_version':5,'operation':'ARCHIVE'});assert archived.status_code==200
        replay=client.post(action,json=one).json();assert replay['replayed'] and replay['goal']['phase']=='ARCHIVED' and replay['receipt']==accepted.json()['receipt']
        final=client.get(path).json();assert final['version']==6 and len(final['completions'])==2
        assert all(e['status']=='CURRENT' for c in final['completions'] for e in c['evidence'])
        assert 'Private shared acceptance source' not in json.dumps(final) and 'Private fixture direct acceptance source' not in json.dumps(final)
        after=client.get('/__e2e/goal-runtime').json();assert after['pid']!=before['pid'] and after['commit']==before['commit'] and after['source_sha256']==before['source_sha256']
        assert after['model_calls']==0 and after['external_calls']==0 and after['source_event_count']==before['source_event_count']
    destination=Path(os.getenv('MODEL_BUDGET_EVIDENCE_DIR',str(tmp_path)));destination.mkdir(parents=True,exist_ok=True)
    (destination/'person-goals-http-restart.json').write_text(json.dumps({'before':before,'after':after,'final':final,'created':created.json(),'first_acceptance':accepted.json(),'replayed':replay,'ports_closed':True,'external_calls':0},indent=2),encoding='utf-8')
