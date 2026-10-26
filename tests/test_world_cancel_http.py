import json
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_model_budget_http import server

@pytest.mark.parametrize('kind,stage',[('WEB','PLAN'),('RSS','PLAN'),('WEB','APPRAISAL'),('RSS','APPRAISAL'),('WEB','CONTINUATION')])
def test_actual_http_cancel_fences_late_result_and_survives_restart(tmp_path,kind,stage):
    env={'RECOVERY_KIND':kind,'CANCEL_STAGE':stage}
    with server(tmp_path,app_module='e2e_world_cancel_app',extra_env=env) as client:
        with ThreadPoolExecutor(1) as pool:
            running=pool.submit(client.post,'/v1/world/dev/browse/rin')
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                before=client.get('/__e2e/cancel-state').json()
                if before['entered']:break
                time.sleep(.02)
            assert before['entered']
            opportunity=before['opportunities'][0]['opportunity_id']
            url='/v1/world/activity/cancel/'+opportunity
            try:
                assert client.post(url,json={'character_id':'momo'}).status_code==404
                assert client.post(url,json={'character_id':'rin','reason':'untrusted'}).status_code==422
                cancellation=client.post(url,json={'character_id':'rin'})
                assert cancellation.status_code==200 and cancellation.json()['cancelled']
                assert client.post(url,json={'character_id':'rin'}).json()==cancellation.json()
            finally:client.post('/__e2e/cancel-release')
            late=running.result()
            assert late.status_code==409 and late.json()['detail']=='WORLD_OPPORTUNITY_CANCELLED'
        after=client.get('/__e2e/cancel-state').json()
        assert after['events']==[] and len(after['models'])==(1 if stage=='PLAN' else 2)
        assert all(x['status']=='SUCCESS' for x in after['models'])
        assert after['world'][0]['phase']==('PLANNING' if stage=='PLAN' else 'CONT_MODEL:2' if stage=='CONTINUATION' else 'APPRAISING')
        assert client.post('/v1/world/activity/recovery/'+opportunity).status_code==409
    with server(tmp_path,app_module='e2e_world_cancel_app',extra_env={**env,'RECOVERY_NO_PROVIDERS':'1'}) as client:
        assert client.post(url,json={'character_id':'rin'}).json()==cancellation.json()
        assert client.post('/v1/world/activity/recovery/'+opportunity).status_code==409
        restarted=client.get('/__e2e/cancel-state').json()
        assert restarted['pid']!=after['pid'] and restarted['commit']==after['commit']
        assert restarted['source_sha256']==after['source_sha256'] and restarted['models']==after['models']
        assert restarted['events']==[] and restarted['cancellations']==after['cancellations']
    calls=[json.loads(x) for x in (tmp_path/'model-calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(calls)==(1 if stage=='PLAN' else 2) and all(x['pid']==after['pid'] for x in calls)
    dest=Path(os.getenv('MODEL_BUDGET_EVIDENCE_DIR',str(tmp_path)));dest.mkdir(parents=True,exist_ok=True)
    (dest/f'world-cancel-{kind}-{stage}.json').write_text(json.dumps({'before':before,'after':after,'restarted':restarted,
        'cancellation':cancellation.json(),'late_status':late.status_code,'actual_local_calls':calls,'external_calls':0,'owned_ports_closed':True},indent=2),encoding='utf-8')

