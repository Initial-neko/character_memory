"""Isolated real HTTP/restart check for W4A; local model, no formal data."""
from __future__ import annotations
import argparse,json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from urllib.request import Request,urlopen
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from character_memory.domain.models import Event,EventType,Memory,PersonReaction,ActionDecision,ActionType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.memory.embedding import DeterministicEmbedding
NOW=datetime(2026,10,26,tzinfo=timezone.utc)
SIM={'experience_scope':{'schema_version':1,'reality':'SIMULATED','activity_id':'practice','branch_id':'unused'}}

def serve(db,port):
    import uvicorn
    from character_memory.api import create_api
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory.memory.recall import VectorRecall
    from character_memory.llm.client import PersonModel
    class CheckedModel(PersonModel):
        def react(self,context):
            with (Path(db).parent/'model-calls.jsonl').open('a',encoding='utf-8') as audit:audit.write(json.dumps({'pid':os.getpid(),'leaked':'SIMULATION_SECRET' in context})+'\n')
            message='SCOPE_LEAK' if 'SIMULATION_SECRET' in context else 'REAL_CONTEXT_SAFE'
            return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE,message=message)])
    store=SQLiteStore(db);emb=DeterministicEmbedding();clock=FixedClock(NOW)
    person=PersonRuntime(store,VectorRecall(store,emb),emb,CheckedModel(),'isolated persona')
    settings=SimpleNamespace(chat_model='local-scope-check',embedding_provider='deterministic',embedding_model='deterministic',db_path=str(db),base_url='offline')
    app=create_api(bundle=SimpleNamespace(settings=settings,chat=ChatService(store,person,clock),store=store,clock=clock,runtime=person))
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')

def validate(output):
    with tempfile.TemporaryDirectory(prefix='cm-scope-') as folder:
        root=Path(folder);db=root/'scope.db';store=SQLiteStore(db);emb=DeterministicEmbedding()
        for i in range(8):store.append_event(Event(character_id='rin',event_type=EventType.USER_MESSAGE,event_time=NOW-timedelta(seconds=100-i),content=f'real-practice-{i}'))
        for i in range(20):
            for kind in [EventType.USER_MESSAGE,EventType.WORLD_OBSERVATION]:store.append_event(Event(character_id='rin',event_type=kind,event_time=NOW-timedelta(seconds=50-i),content='practice SIMULATION_SECRET',metadata=SIM))
        for metadata,text in [({},'real practice memory'),(SIM,'SIMULATION_SECRET')]:store.add_memory(Memory(character_id='rin',content=text,event_time=NOW-timedelta(minutes=1),metadata=metadata,pinned=True,importance=1,embedding=emb.embed('practice')))
        store.close();pids=[];statuses=[]
        for cycle in range(2):
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            with (root/'server.log').open('ab') as log:
                proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve',str(db),'--port',str(port)],cwd=ROOT,env={**os.environ,'PYTHONIOENCODING':'utf-8'},stdout=log,stderr=log)
                pids.append(proc.pid)
                try:
                    for _ in range(100):
                        if proc.poll() is not None:raise RuntimeError('isolated API exited: '+(root/'server.log').read_text(errors='replace')[-3000:])
                        try:
                            with urlopen(f'http://127.0.0.1:{port}/v1/chat/history?character_id=rin&conversation_id=scope',timeout=1):break
                        except Exception:time.sleep(.1)
                    request=Request(f'http://127.0.0.1:{port}/v1/chat',data=json.dumps({'character_id':'rin','conversation_id':'scope','message':'practice'}).encode(),headers={'Content-Type':'application/json'})
                    with urlopen(request,timeout=10) as response:
                        value=response.read().decode();statuses.append(response.status)
                        assert response.status==200 and 'REAL_CONTEXT_SAFE' in value and 'SIMULATION_SECRET' not in value and 'SCOPE_LEAK' not in value
                finally:
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        calls=[json.loads(line) for line in (root/'model-calls.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(calls)==2 and not any(c['leaked'] for c in calls)
        store=SQLiteStore(db)
        audit=store.conn.execute("SELECT COUNT(*) FROM events WHERE content='practice SIMULATION_SECRET'").fetchone()[0]
        assert audit==40 and len(store.list_memories('rin'))==2
        store.close()
        result={'schema_version':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),'status':'PASS','api_pids':pids,'http_statuses':statuses,'local_model_calls':len(calls),'paid_model_calls':0,'preserved_simulated_audit_facts':audit,'coverage':'real HTTP context isolation across API restart; no Activity execution or human quality claim'}
        if output:Path(output).write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve');parser.add_argument('--port',type=int);parser.add_argument('--output');args=parser.parse_args()
    if args.serve:serve(args.serve,args.port)
    else:validate(args.output)
