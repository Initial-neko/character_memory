"""Real isolated HTTP acceptance replay validation; execution recovery is not proven.

Uses production Direct/Group routes, temporary personas/database and a local
fixed-response provider. No paid model, formal config or formal DB is used.
"""
from __future__ import annotations
import argparse
from contextlib import closing,contextmanager
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib,json,os
from pathlib import Path
import socket,sqlite3,subprocess,sys,tempfile,threading,time
from urllib.request import Request,urlopen
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def request(origin,path,body=None):
    req=Request(origin+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=10) as response:
        return response.status,json.load(response)


@contextmanager
def serve(config,folder):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONIOENCODING':'utf-8','OPENCODE_GO_API_KEY':'unused-local-fixture',
        'CHARACTER_MEMORY_DB_PATH':str(folder/'probe.db'),'CHARACTER_MEMORY_EAGER_WARMUP':'0'}
    with (folder/'server.log').open('ab') as log:
        proc=subprocess.Popen([sys.executable,__file__,'--serve',str(config),'--port',str(port)],cwd=folder,env=env,stdout=log,stderr=log)
        origin=f'http://127.0.0.1:{port}'
        try:
            for _ in range(150):
                if proc.poll() is not None: raise RuntimeError((folder/'server.log').read_text(encoding='utf-8',errors='replace'))
                try: request(origin,'/health'); break
                except OSError: time.sleep(.1)
            else: raise TimeoutError('API startup')
            yield origin,proc.pid
        finally:
            proc.terminate()
            try: proc.wait(8)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(5)


def wait_trace(db,event_id,scope):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        with closing(sqlite3.connect(db)) as conn:
            found=conn.execute('SELECT COUNT(*) FROM runtime_traces WHERE source_event_id=?' if scope=='DIRECT' else 'SELECT COUNT(*) FROM conversation_runtime_traces WHERE source_conversation_event_id=?',(event_id,)).fetchone()[0]
        if found >= (1 if scope=='DIRECT' else 2): return
        time.sleep(.1)
    raise TimeoutError(f'missing trace {event_id}')


parser=argparse.ArgumentParser()
parser.add_argument('--serve',type=Path);parser.add_argument('--port',type=int);parser.add_argument('--output',type=Path)
args=parser.parse_args()
if args.serve:
    from character_memory.api import create_api
    from character_memory.async_web import attach_async_routes
    from character_memory.group_web import attach_group_routes
    import uvicorn
    app=create_api(str(args.serve));attach_group_routes(app,str(args.serve));attach_async_routes(app)
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
else:
    calls=[]
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            calls.append({'path':self.path,'body_sha256':hashlib.sha256(body).hexdigest()})
            result={'id':'fixture','object':'chat.completion','created':0,'model':'local-fixed-response',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"actions":[]}'}}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            data=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def log_message(self,*a): pass
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    evidence={'schema_version':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
        'paid_model_calls':0,'provider':'isolated local fixed response; no model semantics claimed','attempts':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='cm-w2-probe-') as temporary:
            folder=Path(temporary)
            for name in ['momo','rin']:
                path=folder/'personas'/name;path.mkdir(parents=True)
                (path/'persona.yaml').write_text(f'id: {name}\nname: {name}\n',encoding='utf-8')
            config=folder/'config.yaml'
            config.write_text(json.dumps({'api_key':'unused-local-fixture','base_url':f'http://127.0.0.1:{provider.server_port}/v1',
                'embedding_provider':'deterministic','db_path':str(folder/'probe.db'),
                'persona_path':str(folder/'personas/momo/persona.yaml'),'proactive_dispatch_enabled':False,'llm_attempts':1}),encoding='utf-8')
            from character_memory.storage.sqlite import SQLiteStore
            from character_memory.group_store import GroupRepository
            from PIL import Image
            from io import BytesIO
            import base64
            store=SQLiteStore(folder/'probe.db')
            group=GroupRepository(store).create_group('HTTP receipts',['momo','rin'],datetime.now(timezone.utc))
            store.close()
            image=BytesIO();Image.new('RGB',(3,3),'red').save(image,format='PNG')
            attachment={'filename':'fixture.png','data_url':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()}
            cases=[]
            for scope in ['DIRECT','GROUP']:
                for with_image in [False,True]:
                    body={'request_id':f'w2-repeat-{scope}-{with_image}','message':'Same accepted message'}
                    if scope=='DIRECT': body.update(character_id='momo',conversation_id='w2-same-conversation')
                    if with_image: body['image']=attachment
                    cases.append((scope,'/v1/chat/messages' if scope=='DIRECT' else f'/v1/groups/{group.id}/messages',body))
            for cycle in range(2):
                with serve(config,folder) as (origin,pid):
                    for scope,url,body in cases:
                        for number in range(2 if cycle==0 else 1):
                            status,result=request(origin,url,body)
                            assert status==202,result
                            wait_trace(folder/'probe.db',result['event_id'],scope)
                            evidence['attempts'].append({'cycle':cycle,'pid':pid,'scope':scope,'request_id':body['request_id'],
                                'http_status':status,'event_id':result['event_id'],'receipt':result['request_receipt'],'provider_calls_after':len(calls)})
            with closing(sqlite3.connect(folder/'probe.db')) as conn:
                evidence['direct_source_facts']=conn.execute("SELECT COUNT(*) FROM events WHERE event_type='USER_MESSAGE'").fetchone()[0]
                evidence['group_source_facts']=conn.execute("SELECT COUNT(*) FROM conversation_events WHERE event_type='USER_MESSAGE'").fetchone()[0]
                evidence['media_assets']=conn.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0]
            evidence['provider_calls']=len(calls)
            for scope,url,body in cases:
                ids={item['event_id'] for item in evidence['attempts'] if item['request_id']==body['request_id']}
                assert len(ids)==1,evidence
            assert evidence['direct_source_facts']==2 and evidence['group_source_facts']==2,evidence
            assert evidence['media_assets']==2 and evidence['provider_calls']==6,evidence
            evidence['status']='PASS'
            evidence['coverage']='Direct/Group text+image acceptance; three retries including restart; execution recovery NOT tested'
    finally:
        provider.shutdown();provider.server_close()
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))
