"""Canonical model over actual API HTTP, durable history and two processes."""
import json,os
from pathlib import Path
from test_model_budget_http import server

def test_retired_model_alias_over_http_and_restart(tmp_path):
    records=[]
    with server(tmp_path,app_module='e2e_provider_retirement_app') as client:
        before=client.get('/__e2e/provider-model').json();records.append(before)
        assert before['configured_model']=='deepseek-flash'
        assert before['actual_model']==before['actual_vision_model']=='deepseek-v4.1-flash'
        assert before['calls']==[]
        response=client.post('/v1/chat',json={'character_id':'rin','message':'provider migration check'})
        assert response.status_code==200,response.text
        history=client.get('/v1/chat/history',params={'character_id':'rin'}).json()['messages']
        assert len([r for r in history if r['role']=='assistant' and r['content']=='Compatibility fixture reply.'])==1
        source=next(r for r in history if r['role']=='user')
        trace=client.get('/v1/traces/'+str(source['id']))
        assert trace.status_code==200 and 'deepseek-v4.1-flash' in json.dumps(trace.json())
        first=client.get('/__e2e/provider-model').json()
        assert len(first['calls'])==1 and first['configured_model']=='deepseek-flash'
    with server(tmp_path,app_module='e2e_provider_retirement_app') as client:
        resumed=client.get('/__e2e/provider-model').json();records.append(resumed)
        assert resumed['pid']!=before['pid'] and resumed['commit']==before['commit']
        assert resumed['source_sha256']==before['source_sha256'] and len(resumed['calls'])==1
        assert client.get('/v1/chat/history',params={'character_id':'rin'}).json()['messages']==history
        response=client.post('/v1/chat',json={'character_id':'rin','message':'after restart'})
        assert response.status_code==200,response.text
        final=client.get('/__e2e/provider-model').json()
        assert len(final['calls'])==2 and len({r['pid'] for r in final['calls']})==2
        assert all(r['model']=='deepseek-v4.1-flash' and r['external_calls']==0 for r in final['calls'])
    out=Path(os.getenv('MODEL_BUDGET_EVIDENCE_DIR',str(tmp_path)));out.mkdir(parents=True,exist_ok=True)
    (out/'provider-model-retirement-http.json').write_text(json.dumps({'runtimes':records,'first':first,'final':final,'history':history,'trace':trace.json(),'ports_closed':True,'external_calls':0},ensure_ascii=False,indent=2),encoding='utf-8')