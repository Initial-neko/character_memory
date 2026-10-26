"""Real isolated Direct/Group HTTP and restart checks; no external model requests."""
import argparse,json,os,shutil,socket,subprocess,sys,tempfile,threading,time,re
from pathlib import Path
from contextlib import closing
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from validate_experience_profile import request
ROOT=Path(__file__).resolve().parents[1]

def serve(port):
    import uvicorn
    from types import SimpleNamespace
    sys.path.insert(0,str(ROOT/'tests'))
    import e2e_realtime_app as fixture
    fixture.app.state.character_memory.reaction_scheduler.quiet_seconds=60
    fixture.app.state.character_memory.reaction_scheduler.max_burst_seconds=60
    def structured(prompt,schema,session):
        row={'pid':os.getpid(),'channel':'GROUP' if '"type": "GROUP"' in prompt else 'DIRECT','failed':'FAIL_SUGGESTION' in prompt}
        with Path(os.environ['SUGGESTION_CALL_LOG']).open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
        time.sleep(.3)
        if row['failed']:raise RuntimeError('fixed local provider failure')
        return SimpleNamespace(value=schema.model_validate({'suggestions':[
            {'intent':'clarify','text':'你更在意哪一部分？'},
            {'intent':'feeling','text':'我有点紧张，想慢慢来。'},
            {'intent':'plan','text':'我们先试一个开场吧。'}]}))
    fixture.model.structured_call_for_session=structured
    original_reaction=fixture.model._reaction
    def reaction(context):
        match=re.search(r'profile_version=(\d+)',context)
        row={'pid':os.getpid(),'profile_version':int(match.group(1)) if match else None,'brief':'verbosity=BRIEF' in context}
        with Path(os.environ['SUGGESTION_CALL_LOG']).with_name('normal-contexts.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
        return original_reaction(context)
    fixture.model._reaction=reaction
    Path(os.environ['SUGGESTION_CALL_LOG']).with_name(f'api-{port}.json').write_text(json.dumps({'pid':os.getpid(),'port':port}),encoding='utf-8')
    uvicorn.run(fixture.app,host='127.0.0.1',port=port,log_level='warning')

def validate(output):
    with tempfile.TemporaryDirectory(prefix='cm-suggestions-') as folder:
        root=Path(folder).resolve();personas=root/'personas'
        assert root.parent==Path(tempfile.gettempdir()).resolve() and root.name.startswith('cm-suggestions-')
        for name in ('rin','momo'):
            (personas/name).mkdir(parents=True);shutil.copyfile(ROOT/'personas'/name/'persona.yaml',personas/name/'persona.yaml')
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONIOENCODING':'utf-8','CHARACTER_MEMORY_E2E_DB':str(root/'test.db'),
             'CHARACTER_MEMORY_E2E_MEDIA':str(root/'media'),'CHARACTER_MEMORY_E2E_PERSONA_ROOT':str(personas),'SUGGESTION_CALL_LOG':str(root/'calls.jsonl')}
        processes=[];pids=[];launchers=[]
        def start():
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            log=(root/f'server-{port}.log').open('ab');tree=None;options={}
            if os.name=='nt':
                from character_memory.runtime.process_tree import WindowsProcessTree
                tree=WindowsProcessTree();options['creationflags']=0x00000004|0x08000000
            proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,**options)
            processes.append((proc,log,tree,port));launchers.append(proc.pid)
            if tree:tree.attach_and_resume(proc)
            origin=f'http://127.0.0.1:{port}'
            for _ in range(150):
                if proc.poll() is not None:raise RuntimeError('isolated API exited: '+(root/f'server-{port}.log').read_text(errors='replace')[-2000:])
                try:
                    if request(origin,'/v2/me/experience-profile')[0]==200:
                        pids.append(json.loads((root/f'api-{port}.json').read_text())['pid']);return origin
                except Exception:time.sleep(.1)
            raise RuntimeError('isolated API readiness timeout')
        def stop():
            for proc,log,tree,port in processes:
                if tree:tree.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                log.close()
                for _ in range(50):
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1',port))!=0:break
                    time.sleep(.02)
                else:raise AssertionError('owned API port remains open')
            processes.clear()
        def calls():return [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
        def latest_direct(base):
            rows=request(base,'/v1/chat/history?character_id=rin')[1]['messages'];return {'kind':'EVENT','id':rows[-1]['id']},len(rows)
        try:
            origins=[start(),start()];base=origins[0]
            assert request(base,'/v1/chat','POST',{'character_id':'rin','message':'suggestion fixture'})[0]==200
            ref,before=latest_direct(base);payload={'request_id':str(uuid4()),'context_ref':ref,'expected_profile_version':0}
            gate=threading.Barrier(2)
            def race(index):gate.wait();return request(origins[index],'/v2/reply-suggestions','POST',payload)
            with ThreadPoolExecutor(2) as pool:results=list(pool.map(race,[0,1]))
            statuses=[r[0] for r in results];assert sorted(statuses)==[200,202],results
            batch=next(r[1] for r in results if r[0]==200)
            assert len(batch['suggestions'])==3 and len(calls())==1
            assert request(base,'/v2/reply-suggestions','POST',payload)[1]==batch
            assert latest_direct(base)[1]==before
            direct_send={'request_id':str(uuid4()),'character_id':'rin','message':batch['suggestions'][0]['text'],
                         'reply_suggestion':{'batch_id':batch['batch_id'],'suggestion_id':batch['suggestions'][0]['id']}}
            accepted=request(base,'/v1/chat/messages','POST',direct_send);assert accepted[0]==202,accepted
            direct_signal=accepted[1]['reply_suggestion_signal'];assert direct_signal['kind']=='SELECT' and direct_signal['source_ref']['id']==accepted[1]['event_id']
            assert request(base,'/v1/chat/messages','POST',direct_send)[1]['reply_suggestion_signal']==direct_signal and len(calls())==1
            assert request(base,'/v2/me/experience-profile','PATCH',{'expected_version':0,'changes':{'tone':'CUTE'}})[0]==200
            assert request(base,'/v2/reply-suggestions/'+batch['batch_id'])[0]==409
            group=request(base,'/v1/groups','POST',{'name':'Suggestions','member_ids':['rin','momo']})[1]['group']['id']
            sent=request(base,f'/v1/groups/{group}/messages','POST',{'message':'group suggestion fixture','request_id':str(uuid4())});assert sent[0]==202,sent
            # The real accepted group fact is the explicit context anchor.
            ref={'kind':'CONVERSATION_EVENT','conversation_id':group,'id':sent[1]['event_id']}
            group_payload={'request_id':str(uuid4()),'context_ref':ref,'expected_profile_version':1}
            made=request(base,'/v2/reply-suggestions','POST',group_payload);assert made[0]==200,made
            group_batch=made[1];assert len(calls())==2
            stop();base=start()
            assert request(base,'/v2/reply-suggestions/'+group_batch['batch_id'])[1]==group_batch and len(calls())==2
            group_send={'request_id':str(uuid4()),'message':'我想修改建议后再说。',
                        'reply_suggestion':{'batch_id':group_batch['batch_id'],'suggestion_id':group_batch['suggestions'][0]['id']}}
            accepted=request(base,f'/v1/groups/{group}/messages','POST',group_send);assert accepted[0]==202,accepted
            edit_signal=accepted[1]['reply_suggestion_signal'];assert edit_signal['kind']=='EDIT' and edit_signal['source_ref']['conversation_id']==group
            assert request(base,f'/v1/groups/{group}/messages','POST',group_send)[1]['reply_suggestion_signal']==edit_signal
            next_ref={'kind':'CONVERSATION_EVENT','conversation_id':group,'id':accepted[1]['event_id']}
            def make_group(ref,version):
                made=request(base,'/v2/reply-suggestions','POST',{'request_id':str(uuid4()),'context_ref':ref,'expected_profile_version':version})
                assert made[0]==200,made
                return made[1]
            free_batch=make_group(next_ref,1)
            free_send={'request_id':str(uuid4()),'message':'这次我想完全自由输入。','reply_suggestion':{'batch_id':free_batch['batch_id']}}
            accepted=request(base,f'/v1/groups/{group}/messages','POST',free_send);assert accepted[0]==202,accepted
            free_signal=accepted[1]['reply_suggestion_signal'];assert free_signal['kind']=='FREE_INPUT'
            next_ref={'kind':'CONVERSATION_EVENT','conversation_id':group,'id':accepted[1]['event_id']}
            feedback_batch=make_group(next_ref,1)
            ignored={'request_id':str(uuid4()),'batch_id':feedback_batch['batch_id'],'kind':'IGNORE'}
            ignore_result=request(base,'/v2/reply-suggestions/feedback','POST',ignored);assert ignore_result[0]==200,ignore_result
            assert request(base,'/v2/reply-suggestions/feedback','POST',ignored)[1]==ignore_result[1]
            assert request(base,'/v2/me/experience-profile')[1]['version']==1
            feedback={'request_id':str(uuid4()),'batch_id':feedback_batch['batch_id'],'kind':'EXPLICIT_FEEDBACK','comment':'希望简短一点',
                      'expected_profile_version':1,'preference_changes':{'verbosity':'BRIEF'}}
            feedback_result=request(base,'/v2/reply-suggestions/feedback','POST',feedback);assert feedback_result[0]==200,feedback_result
            assert feedback_result[1]['applied_profile_version']==2
            assert request(base,'/v2/reply-suggestions/feedback','POST',feedback)[1]==feedback_result[1]
            assert request(base,'/v2/me/experience-profile')[1]['version']==2
            archive_batch=make_group(next_ref,2)
            assert request(base,f'/v1/groups/{group}/archive','POST',{})[0]==200
            assert request(base,'/v2/reply-suggestions/'+archive_batch['batch_id'])[0]==409
            assert request(base,'/v1/chat','POST',{'character_id':'rin','message':'FAIL_SUGGESTION'})[0]==200
            normal_rows=[json.loads(line) for line in (root/'normal-contexts.jsonl').read_text().splitlines()]
            assert normal_rows[-1]['profile_version']==2 and normal_rows[-1]['brief'] is True
            ref,_=latest_direct(base);failed={'request_id':str(uuid4()),'context_ref':ref,'expected_profile_version':2}
            assert request(base,'/v2/reply-suggestions','POST',failed)[0]==502 and len(calls())==6
            stop();base=start()
            assert request(base,'/v2/reply-suggestions','POST',failed)[0]==502 and len(calls())==6
            assert request(base,'/v2/me/experience-profile?expected_version=2','DELETE')[1]['version']==3
            assert request(base,'/v2/reply-suggestions/feedback','POST',feedback)[1]==feedback_result[1]
            profile=request(base,'/v2/me/experience-profile')[1];assert profile['version']==3 and profile['preferences']=={}
            assert request(base,'/v1/chat','POST',{'character_id':'rin','message':'after clear'})[0]==200
            normal_rows=[json.loads(line) for line in (root/'normal-contexts.jsonl').read_text().splitlines()]
            assert normal_rows[-1]['profile_version'] is None and normal_rows[-1]['brief'] is False
            signals=request(base,'/v2/reply-suggestions/'+feedback_batch['batch_id']+'/signals')[1]['signals'];assert len(signals)==2
            # Author governance does not need a live/still-current draft context.
            other=start();governance_path='/v2/reply-suggestions/feedback/'+feedback_result[1]['id']
            changes=[{'request_id':str(uuid4()),'expected_revision':1,'comment':'更正的明确反馈 '+str(i)} for i in range(2)]
            change_gate=threading.Barrier(2)
            def race_change(index):
                change_gate.wait();return request([base,other][index],governance_path,'PATCH',changes[index])
            with ThreadPoolExecutor(2) as pool:change_results=list(pool.map(race_change,[0,1]))
            change_statuses=[r[0] for r in change_results];assert sorted(change_statuses)==[200,409],change_results
            winner=next(i for i,r in enumerate(change_results) if r[0]==200);correction=change_results[winner][1];assert correction['revision']==2 and 'comment' not in correction
            withdrawal={'request_id':str(uuid4()),'expected_revision':2}
            withdrawn=request(base,governance_path+'/withdraw','POST',withdrawal);assert withdrawn[0]==200 and withdrawn[1]['revision']==3,withdrawn
            for chosen in (direct_signal,edit_signal):
                result=request(base,'/v2/reply-suggestions/feedback/'+chosen['id']+'/withdraw','POST',{'request_id':str(uuid4()),'expected_revision':1})
                assert result[0]==200,result
            old_accept=request(base,'/v1/chat/messages','POST',direct_send)
            assert old_accept[0]==202 and old_accept[1]['reply_suggestion_signal']['withdrawn'] is True
            assert old_accept[1]['reply_suggestion_signal']['source_ref']==direct_signal['source_ref']
            stop();base=start()
            assert request(base,governance_path,'PATCH',changes[winner])[1]==correction
            assert request(base,governance_path+'/withdraw','POST',withdrawal)[1]==withdrawn[1]
            assert request(base,governance_path,'PATCH',{'request_id':str(uuid4()),'expected_revision':3,'comment':'cannot revive'})[0]==409
            replay=request(base,'/v2/reply-suggestions/feedback','POST',feedback)
            assert replay[0]==200 and replay[1]['withdrawn'] is True and replay[1]['comment'] is None
            assert request(base,'/v2/me/experience-profile')[1]['preferences']=={}
            active=request(base,'/v2/reply-suggestions/'+feedback_batch['batch_id']+'/signals?active_only=true')[1]['signals']
            assert len(active)==1 and active[0]['kind']=='IGNORE'
            import sqlite3
            with closing(sqlite3.connect((root/'test.db').as_uri()+'?mode=ro',uri=True)) as audit:
                change_count=audit.execute('SELECT COUNT(*) FROM reply_suggestion_signal_changes').fetchone()[0]
                logical='\n'.join(audit.iterdump())
                assert change_count==4
                assert '希望简短一点' not in logical and '更正的明确反馈' not in logical
            governance={'race_http_statuses':change_statuses,'correction_revision':2,'withdrawal_revision':3,'mutation_receipts':change_count,
                        'old_acceptance_replay_withdrawn':True,'restarted_old_changes_no_resurrection':True,'old_logical_plaintext_absent':True,
                        'active_feedback_kinds':[row['kind'] for row in active]}
            assert all(row['pid'] in pids for row in calls())
            assert all(row['pid'] in pids for row in normal_rows)
            assert all(p.read_bytes()==(ROOT/'personas'/p.parent.name/'persona.yaml').read_bytes() for p in personas.glob('*/persona.yaml'))
            stop()
            report={'schema_version':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),'status':'PASS','api_pids':pids,'launcher_pids':launchers,
                    'race_http_statuses':statuses,'suggestion_calls':calls(),'external_model_calls':0,'generation_message_count_unchanged':True,'local_model_calls':len(calls())+len(normal_rows),
                    'profile_and_archive_invalidation':True,'choice_signals':[direct_signal,edit_signal,free_signal],'explicit_feedback_signals':signals,'cleared_profile_not_resurrected':True,'normal_contexts':normal_rows,'restarted_cache_and_failure_no_replay':True,'owned_ports_closed':True,'persona_files_unchanged':True,
                    'governance':governance,'scope':'deterministic provider proves engineering only; no semantic quality or human acceptance'}
        finally:stop()
    assert not root.exists(), 'owned temporary validation directory remains'
    report['temporary_directory_removed']=True
    if output:Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--port',type=int);parser.add_argument('--output');args=parser.parse_args()
    if args.serve:serve(args.port)
    else:validate(args.output)
