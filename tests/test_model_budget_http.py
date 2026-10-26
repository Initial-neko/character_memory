"""Real HTTP, SQLite persistence and two owned application processes."""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import httpx


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def server(root):
    for name in ('rin','momo'):
        target=root/'personas'/name
        target.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/'personas'/name/'persona.yaml',target/'persona.yaml')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONIOENCODING':'utf-8',
         'CHARACTER_MEMORY_E2E_DB':str(root/'budget.db'),
         'CHARACTER_MEMORY_E2E_MEDIA':str(root/'media'),
         'CHARACTER_MEMORY_E2E_PERSONA_ROOT':str(root/'personas'),
         'MODEL_BUDGET_CALL_LOG':str(root/'model-calls.jsonl')}
    tree=None
    options={}
    if os.name=='nt':
        from character_memory.runtime.process_tree import WindowsProcessTree
        tree=WindowsProcessTree()
        options['creationflags']=0x00000004|0x08000000
    with (root/f'api-{port}.log').open('wb') as log:
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','e2e_model_budget_app:app',
            '--app-dir',str(ROOT/'tests'),'--host','127.0.0.1','--port',str(port),
            '--log-level','warning'],cwd=ROOT,env=env,stdout=log,stderr=log,**options)
        try:
            if tree:tree.attach_and_resume(proc)
            origin=f'http://127.0.0.1:{port}'
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if proc.poll() is not None:
                    raise RuntimeError((root/f'api-{port}.log').read_text(encoding='utf-8',errors='replace'))
                try:
                    with urlopen(origin+'/health',timeout=.5) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('budget HTTP readiness timeout')
            with httpx.Client(base_url=origin,timeout=15) as client:
                yield client
        finally:
            if tree:tree.close()
            if proc.poll() is None:proc.terminate()
            try:proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait(timeout=3)
            for _ in range(50):
                with socket.socket() as probe:
                    if probe.connect_ex(('127.0.0.1',port))!=0:break
                time.sleep(.02)
            else:raise AssertionError('owned budget API port remains open')


def test_actual_http_budget_survives_restart_and_does_not_block_chat(tmp_path):
    pids=[]
    with server(tmp_path) as client:
        pids.append(client.get('/__e2e/model-budget-runtime').json()['pid'])
        before=client.get('/v1/world/activity/status')
        assert before.status_code==200,before.text
        assert not before.json()['model_invocation_budget']['initialized']
        first=client.post('/v1/world/dev/browse/rin')
        assert first.status_code==200,first.text
        assert not first.json()['browsed']
        refused=client.post('/v1/world/dev/browse/rin')
        assert refused.status_code==502 and refused.json()['detail']=='BUDGET_EXHAUSTED'
        first_status=client.get('/v1/world/activity/status').json()['model_invocation_budget']
        assert first_status['characters']==[{'character_id':'rin','used':1,'remaining':0}]
    with server(tmp_path) as client:
        pids.append(client.get('/__e2e/model-budget-runtime').json()['pid'])
        restarted=client.get('/v1/world/activity/status').json()['model_invocation_budget']
        assert restarted==first_status
        refused=client.post('/v1/world/dev/browse/rin')
        assert refused.status_code==502 and refused.json()['detail']=='BUDGET_EXHAUSTED'
        other=client.post('/v1/world/dev/browse/momo')
        assert other.status_code==200,other.text
        assert client.post('/v1/chat',json={'character_id':'rin','message':'预算耗尽仍能聊天'}).status_code==200
        final_status=client.get('/v1/world/activity/status').json()['model_invocation_budget']
        assert final_status['characters']==[
            {'character_id':'momo','used':1,'remaining':0},
            {'character_id':'rin','used':1,'remaining':0}]
    calls=[json.loads(line) for line in (tmp_path/'model-calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(calls)==2 and len(set(pids))==2
    assert {item['pid'] for item in calls}==set(pids)
    assert all(item['external_calls']==0 for item in calls)
    dest=Path(os.getenv('MODEL_BUDGET_EVIDENCE_DIR',str(tmp_path)))
    dest.mkdir(parents=True,exist_ok=True)
    sources=['src/character_memory/runtime/model_execution.py',
             'src/character_memory/world_activity.py','src/character_memory/world_continuation.py',
             'src/character_memory/world_web.py','src/character_memory/config.py']
    source_diff=subprocess.run(['git','diff','--quiet','HEAD','--',*sources],cwd=ROOT)
    assert source_diff.returncode==0, 'served runtime sources differ from the recorded commit'
    porcelain=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).splitlines()
    evidence={'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'dirty':bool(porcelain),'git_status_porcelain':porcelain,
              'runtime_sources_match_commit':True,
              'runtime_sources_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
              'pids':pids,'first_status':first_status,'final_status':final_status,
              'actual_model_invocations':calls,'external_calls':0,'owned_ports_closed':True}
    (dest/'model-budget-http-restart.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
