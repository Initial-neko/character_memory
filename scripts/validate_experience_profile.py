"""Real isolated HTTP/2-process/restart profile check, deterministic local model."""
from __future__ import annotations
import argparse,json,os,shutil,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]

def request(origin,path,method='GET',body=None):
    data=None if body is None else json.dumps(body).encode()
    try:
        with urlopen(Request(origin+path,data=data,method=method,headers={'Content-Type':'application/json'}),timeout=15) as r:return r.status,json.load(r)
    except HTTPError as exc:return exc.code,json.load(exc)

def serve(port):
    import uvicorn,re
    sys.path.insert(0,str(ROOT/'tests'));import e2e_realtime_app as fixture
    original=fixture.model._reaction
    def counted(context):
        version=re.search(r'profile_version=(\d+)',context)
        row={'pid':os.getpid(),'channel':'GROUP' if '# Group Conversation Contract' in context else 'DIRECT','version':int(version.group(1)) if version else None,
             'cute':'tone=CUTE' in context,'natural':'tone=NATURAL' in context,'brief':'verbosity=BRIEF' in context,'detailed':'verbosity=DETAILED' in context}
        with Path(os.environ['PROFILE_CALL_LOG']).open('a',encoding='utf-8') as log:log.write(json.dumps(row)+'\n')
        return original(context)
    fixture.model._reaction=counted
    Path(os.environ['PROFILE_CALL_LOG']).with_name(f'api-{port}.json').write_text(json.dumps({'pid':os.getpid(),'port':port}),encoding='utf-8')
    uvicorn.run(fixture.app,host='127.0.0.1',port=port,log_level='warning')

def validate(output):
    with tempfile.TemporaryDirectory(prefix='cm-profile-') as folder:
        root=Path(folder);personas=root/'personas'
        for name in ['rin','momo']:
            (personas/name).mkdir(parents=True);shutil.copyfile(ROOT/'personas'/name/'persona.yaml',personas/name/'persona.yaml')
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONIOENCODING':'utf-8','CHARACTER_MEMORY_E2E_DB':str(root/'profile.db'),'CHARACTER_MEMORY_E2E_MEDIA':str(root/'media'),'CHARACTER_MEMORY_E2E_PERSONA_ROOT':str(personas),'PROFILE_CALL_LOG':str(root/'calls.jsonl')}
        processes=[];origins=[];pids=[];launcher_pids=[]
        def start():
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            log=(root/f'server-{port}.log').open('ab')
            tree=None;options={}
            if os.name=='nt':
                sys.path.insert(0,str(ROOT/'src'))
                from character_memory.runtime.process_tree import WindowsProcessTree
                tree=WindowsProcessTree();options['creationflags']=0x00000004|0x08000000
            proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,**options)
            processes.append((proc,log,tree,port));launcher_pids.append(proc.pid)
            if tree is not None:tree.attach_and_resume(proc)
            origin=f'http://127.0.0.1:{port}'
            for _ in range(150):
                if proc.poll() is not None:raise RuntimeError('isolated API exited')
                try:
                    if request(origin,'/v2/me/experience-profile')[0]==200:
                        pids.append(json.loads((root/f'api-{port}.json').read_text(encoding='utf-8'))['pid']);return origin
                except Exception:time.sleep(.1)
            raise RuntimeError('isolated API readiness timeout')
        def stop():
            for proc,log,tree,port in processes:
                if tree is not None:tree.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                log.close()
                for _ in range(50):
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1',port))!=0:break
                    time.sleep(.02)
                else:raise AssertionError('owned API port was not closed')
            processes.clear()
        try:
            origins=[start(),start()];base=origins[0]
            assert request(base,'/v2/me/experience-profile')[1]['version']==0
            # Seed one authored feedback row (the same durable shape the feedback
            # route writes) so candidate decisions can be exercised over real HTTP
            # without a model call.
            import sqlite3
            from datetime import datetime,timezone
            seed=sqlite3.connect(root/'profile.db')
            seed.execute("INSERT INTO reply_suggestion_signals(id,owner_id,request_key,request_hash,batch_id,suggestion_id,kind,source_ref_json,comment,profile_version,applied_profile_version,created_at,revision) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ('candidate-seed','local-operator','feedback:seed','seed','00000000-0000-4000-8000-000000000001',None,
                          'EXPLICIT_FEEDBACK',None,'以后回答简短一点',0,None,datetime.now(timezone.utc).isoformat(),1))
            seed.execute("INSERT INTO reply_suggestion_signals(id,owner_id,request_key,request_hash,batch_id,suggestion_id,kind,source_ref_json,comment,profile_version,applied_profile_version,created_at,revision) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ('candidate-conflict','local-operator','feedback:seed2','seed2','00000000-0000-4000-8000-000000000001',None,
                          'EXPLICIT_FEEDBACK',None,'以后详细一点',0,None,datetime.now(timezone.utc).isoformat(),1))
            seed.commit();seed.close()
            listed=request(base,'/v2/me/experience-profile/candidates')
            assert listed[0]==200
            by_id={item['signal_id']:item for item in listed[1]['candidates']}
            assert by_id['candidate-seed']['status']=='PENDING'
            assert by_id['candidate-seed']['evidence'][0]['phrase']=='简短一点'
            assert request(origins[1],'/v2/me/experience-profile/candidates')[1]['candidates']
            gate=threading.Barrier(2)
            def race(index):
                gate.wait();return request(origins[index],'/v2/me/experience-profile','PATCH',{'expected_version':0,'changes':{'tone':['CUTE','NATURAL'][index]}})[0]
            with ThreadPoolExecutor(2) as pool:race_statuses=list(pool.map(race,[0,1]))
            assert sorted(race_statuses)==[200,409]
            assert request(base,'/v2/me/experience-profile','PATCH',{'expected_version':1,'changes':{'tone':'CUTE','energy':'QUIET','verbosity':'BRIEF'}})[1]['version']==2
            # Explicit choices outrank the candidate: the matching one is merely
            # satisfied, the conflicting one is overridden; neither writes.
            after=request(base,'/v2/me/experience-profile/candidates')[1]['candidates']
            same=next(item for item in after if item['signal_id']=='candidate-seed')
            assert same['status']=='SATISFIED' and same['applicable_fields']=={},same
            other=next(item for item in after if item['signal_id']=='candidate-conflict')
            assert other['status']=='OVERRIDDEN',other
            assert request(base,'/v2/me/experience-profile')[1]['preferences']['verbosity']=='BRIEF'
            assert request(origins[1],'/v2/me/experience-profile')[1]['preferences']['tone']=='CUTE'
            assert request(base,'/v1/chat','POST',{'message':'profile direct','character_id':'rin','conversation_id':'profile-direct'})[0]==200
            assert request(origins[1],'/v2/me/experience-profile','PATCH',{'expected_version':2,'changes':{'tone':'NATURAL','verbosity':'DETAILED'}})[1]['version']==3
            group=request(base,'/v1/groups','POST',{'name':'Profile fixture','member_ids':['rin','momo']})[1]['group']['id']
            assert request(base,f'/v1/groups/{group}/messages','POST',{'message':'profile group','request_id':'profile-group-one'})[0]==202
            for _ in range(100):
                rows=[json.loads(l) for l in (root/'calls.jsonl').read_text().splitlines()]
                if len([r for r in rows if r['channel']=='GROUP'])>=2:break
                time.sleep(.1)
            group_rows=[r for r in rows if r['channel']=='GROUP']
            assert len(group_rows)==2 and all(r['version']==3 and r['natural'] and r['detailed'] and not r['cute'] for r in group_rows)
            assert rows[0]['version']==2 and rows[0]['cute'] and rows[0]['brief']
            # Clearing the profile also retires the open interpretation: the old
            # comment cannot propose the cleared value again.
            assert request(base,'/v2/me/experience-profile?expected_version=3','DELETE')[1]['version']==4
            retired={item['signal_id']:item['status'] for item in request(base,'/v2/me/experience-profile/candidates')[1]['candidates']}
            assert retired=={'candidate-seed':'SUPERSEDED','candidate-conflict':'SUPERSEDED'},retired
            assert request(base,'/v2/me/experience-profile/restore','POST',{'expected_version':4,'target_version':2})[0]==404
            assert [r['operation'] for r in request(base,'/v2/me/experience-profile/versions')[1]['versions']]==['CLEAR']
            assert request(base,'/v2/me/experience-profile','PATCH',{'expected_version':4,'changes':{'tone':'CUTE'},'owner_id':'other'})[0]==422
            stop();base=start()
            assert request(base,'/v2/me/experience-profile')[1]['version']==4 and request(base,'/v2/me/experience-profile')[1]['preferences']=={}
            assert request(base,'/v1/chat','POST',{'message':'after profile clear','character_id':'rin','conversation_id':'profile-direct'})[0]==200
            rows=[json.loads(l) for l in (root/'calls.jsonl').read_text().splitlines()]
            assert len(rows)==4 and rows[-1]['version'] is None and all(r['pid'] in pids for r in rows)
            assert all(p.read_bytes()==(ROOT/'personas'/p.parent.name/'persona.yaml').read_bytes() for p in personas.glob('*/persona.yaml'))
            stop()  # Do not write PASS before all owned API ports are closed.
            result={'schema_version':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),'status':'PASS','api_pids':pids,'launcher_pids':launcher_pids,'race_http_statuses':race_statuses,'candidate_views':{'pending':True,'overridden_by_explicit_choice':True,'expired_after_clear':retired},'final_version':4,'restored_after_clear':False,'persona_files_unchanged':True,'owned_ports_closed':True,'local_model_calls':len(rows),'paid_model_calls':0,'model_contexts':rows,'scope':'Direct/Group real HTTP, 2-process CAS/restart and candidate lifecycle; deterministic provider is not semantic quality acceptance'}
            if output:Path(output).write_text(json.dumps(result,indent=2),encoding='utf-8')
            print(json.dumps(result))
        finally:stop()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--port',type=int);parser.add_argument('--output');args=parser.parse_args()
    if args.serve:serve(args.port)
    else:validate(args.output)
