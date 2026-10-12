import json
import pytest
from character_memory.world_recovery import WorldBrowseRecovery
from test_world_recovery import interrupted
from test_world_activity import NOW,make_access
import character_memory.world_activity as world

def web_interrupted(tmp_path,monkeypatch):
    store,access,model=make_access(tmp_path);service=world.WorldActivityService(access,world.WorldPulseRepository(store))
    original=world.retain_world_observation
    monkeypatch.setattr(world,'retain_world_observation',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):service.browse_character('c00',now=NOW,opportunity_id='web-recover')
    monkeypatch.setattr(world,'retain_world_observation',original)
    return store,access,model

def test_recovery_rejects_nonreading_decision(tmp_path,monkeypatch):
    store,_,_=web_interrupted(tmp_path,monkeypatch)
    try:
        row=store.conn.execute("SELECT plan_json FROM world_browse_decisions").fetchone()
        snapshot=json.loads(row[0]);snapshot['decision'].update(choice='NO_ACTION',browse=False)
        store.conn.execute('UPDATE world_browse_decisions SET plan_json=?',(json.dumps(snapshot),));store.conn.commit()
        report=WorldBrowseRecovery(store).inspect()
        assert not report['candidates'][0]['recoverable'],report
        with pytest.raises(ValueError):WorldBrowseRecovery(store).apply('web-recover')
        assert not store.list_events('c00')
    finally:store.close()

def test_recovery_inspection_does_not_echo_private_validation_input(tmp_path,monkeypatch):
    store,_,_=web_interrupted(tmp_path,monkeypatch)
    try:
        store.conn.execute('UPDATE world_browse_decisions SET appraisal_json=?',(json.dumps({'keep':True,'summary':{'PRIVATE_PAYLOAD':'do not expose'}}),));store.conn.commit()
        report=WorldBrowseRecovery(store).inspect()
        assert 'PRIVATE_PAYLOAD' not in json.dumps(report),report
    finally:store.close()

def test_recovery_keeps_actual_observed_time_and_idempotency(tmp_path,monkeypatch):
    store,access,model=web_interrupted(tmp_path,monkeypatch)
    try:
        before=len(model.calls)
        access.require_bundle=lambda:pytest.fail('paid runtime')
        recovery=WorldBrowseRecovery(store);result=recovery.apply('web-recover')
        events=store.list_events('c00');assert len(events)==1 and events[0].event_time==NOW
        assert recovery.apply('web-recover')==result and len(store.list_events('c00'))==1
        assert len(model.calls)==before
    finally:store.close()

def test_rss_second_item_failure_rolls_back_all_first_item_effects(tmp_path,monkeypatch):
    store,_,model,_,_=interrupted(tmp_path,monkeypatch)
    original=world.retain_world_observation;calls=[]
    def failure(*a,**kw):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('second item fail')
        return original(*a,**kw)
    monkeypatch.setattr(world,'retain_world_observation',failure)
    try:
        with pytest.raises(RuntimeError,match='second item fail'):WorldBrowseRecovery(store).apply('recover-rss')
        assert store.list_events('c00')==[]
        assert {r[0] for r in store.conn.execute('SELECT status FROM world_rss_readings')}=={'STARTED'}
        assert store.conn.execute('SELECT phase FROM world_browse_decisions').fetchone()[0]=='APPRAISED'
        assert not store.conn.in_transaction
    finally:store.close()

def test_rss_recovery_rejects_generation_mismatch(tmp_path,monkeypatch):
    store,_,_,_,_=interrupted(tmp_path,monkeypatch)
    try:
        row=store.conn.execute('SELECT request_json FROM capability_executions').fetchone()
        request=json.loads(row[0]);request['arguments']['items'][0]['source_generation']+=1
        store.conn.execute('UPDATE capability_executions SET request_json=?',(json.dumps(request),));store.conn.commit()
        report=WorldBrowseRecovery(store).inspect()
        assert not report['candidates'][0]['recoverable'],report
    finally:store.close()

def test_web_recovery_rejects_plan_constraint_mismatch(tmp_path,monkeypatch):
    store,_,_=web_interrupted(tmp_path,monkeypatch)
    try:
        row=store.conn.execute('SELECT request_json FROM capability_executions').fetchone()
        request=json.loads(row[0]);request['constraints']['max_pages']=4
        store.conn.execute('UPDATE capability_executions SET request_json=?',(json.dumps(request),));store.conn.commit()
        report=WorldBrowseRecovery(store).inspect()
        assert not report['candidates'][0]['recoverable'],report
    finally:store.close()


def claude_interrupted(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_claude_world import Runner
    store,access,model=make_access(tmp_path)
    access.settings=access.settings.model_copy(update={'world_browse_execution_backend':'claude'})
    runner=Runner()
    access.services=SimpleNamespace(claude_runner=runner)
    service=world.WorldActivityService(access,world.WorldPulseRepository(store))
    with monkeypatch.context() as temporary:
        temporary.setattr(world,'retain_world_observation',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('interrupted')))
        with pytest.raises(RuntimeError,match='interrupted'):
            service.browse_character('c00',now=NOW,opportunity_id='claude-recover')
    return store,access,model,runner,service


def test_claude_recovery_uses_saved_policy_and_no_live_runtime(tmp_path,monkeypatch):
    store,access,model,runner,service=claude_interrupted(tmp_path,monkeypatch)
    try:
        recovery=WorldBrowseRecovery(store)
        before=store.conn.total_changes
        assert recovery.inspect()['candidates'][0]['recoverable']
        assert store.conn.total_changes==before
        access.settings.claude_execution_max_turns=1
        access.require_bundle=lambda:pytest.fail('recovery cannot initialize any provider')
        result=service.browse_character('c00',now=NOW,opportunity_id='claude-recover')
        assert result['kept'] and result['source_event_id']
        assert recovery.apply('claude-recover')==result
        assert len(runner.calls)==1 and len(model.calls)==2
        assert len(store.list_events('c00'))==1
    finally:store.close()


@pytest.mark.parametrize('target',['request','snapshot'])
def test_claude_recovery_rejects_changed_policy(tmp_path,monkeypatch,target):
    store,_,_,_,_=claude_interrupted(tmp_path,monkeypatch)
    try:
        if target=='request':
            row=store.conn.execute('SELECT request_json FROM capability_executions').fetchone()
            payload=json.loads(row[0]);payload['constraints']['claude_max_turns']=1
            store.conn.execute('UPDATE capability_executions SET request_json=?',[json.dumps(payload)])
        else:
            row=store.conn.execute('SELECT plan_json FROM world_browse_decisions').fetchone()
            payload=json.loads(row[0]);payload['execution_policy']['claude_model']='changed'
            store.conn.execute('UPDATE world_browse_decisions SET plan_json=?',[json.dumps(payload)])
        store.conn.commit()
        recovery=WorldBrowseRecovery(store)
        assert not recovery.inspect()['candidates'][0]['recoverable']
        with pytest.raises(ValueError,match='INVALID_RECOVERY_RECEIPT'):recovery.apply('claude-recover')
        assert not store.list_events('c00')
    finally:store.close()
