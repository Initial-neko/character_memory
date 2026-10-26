"""Web/RSS production HTTP recovery across real process restarts, no providers."""
import json
import os
from pathlib import Path

import pytest
from test_model_budget_http import server


@pytest.mark.parametrize('kind',['WEB','RSS'])
def test_appraising_recovery_across_actual_http_restart(tmp_path,kind):
    with server(tmp_path,app_module='e2e_appraisal_recovery_app',extra_env={'RECOVERY_KIND':kind}) as client:
        response=client.post('/v1/world/dev/browse/rin')
        assert response.status_code==502 and 'appraisal persistence interrupted' in response.text
        candidate=client.get('/v1/world/activity/recovery').json()['candidates'][0]
        assert candidate['recoverable'] and candidate['requires_explicit'] and candidate['phase']=='APPRAISING'
        opportunity=candidate['opportunity_id']
        before=client.get('/__e2e/recovery-runtime').json()
        assert before['events']==[] and len(before['models'])==2
        assert all(x['status']=='SUCCESS' for x in before['models'])
    with server(tmp_path,app_module='e2e_appraisal_recovery_app',extra_env={'RECOVERY_KIND':kind,'RECOVERY_NO_PROVIDERS':'1'}) as client:
        inspection=client.get('/v1/world/activity/recovery')
        assert inspection.status_code==200 and inspection.json()['candidates'][0]['recoverable']
        first=client.post('/v1/world/activity/recovery/'+opportunity)
        assert first.status_code==200,first.text
        replay=client.post('/v1/world/activity/recovery/'+opportunity)
        assert replay.status_code==200 and replay.json()==first.json()
        assert client.get('/v1/world/activity/recovery').json()['candidates']==[]
        after=client.get('/__e2e/recovery-runtime').json()
        assert after['no_providers'] and after['pid']!=before['pid']
        assert after['commit']==before['commit'] and after['source_sha256']==before['source_sha256']
        assert after['world'][0]['phase']=='APPLIED' and len(after['events'])==1
        assert after['events'][0]['at']==before['world'][0]['started_at']
        assert after['models']==before['models']
    calls=[json.loads(line) for line in (tmp_path/'model-calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(calls)==2 and all(x['pid']==before['pid'] for x in calls)
    assert all(x['external_calls']==0 for x in calls)
    dest=Path(os.getenv('MODEL_BUDGET_EVIDENCE_DIR',str(tmp_path)));dest.mkdir(parents=True,exist_ok=True)
    (dest/f'appraising-recovery-{kind}.json').write_text(json.dumps({
        'before':before,'after':after,'actual_local_calls':calls,'owned_ports_closed':True,
        'external_calls':0,'first_result':first.json(),'replayed_result':replay.json()},ensure_ascii=False,indent=2),encoding='utf-8')
