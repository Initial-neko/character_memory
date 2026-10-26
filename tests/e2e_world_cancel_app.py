"""Real World routes with bounded local provider gates, no external services."""
import os
import threading
from character_memory.world_activity import WorldPulseRepository
import e2e_appraisal_recovery_app as base

app=base.app
base.fixture.store.conn.execute('DROP TRIGGER IF EXISTS interrupt_appraisal')
base.fixture.store.conn.commit()
entered=threading.Event();release=threading.Event()
original=base.fixture.model.structured_for_session
stage=os.getenv('CANCEL_STAGE','PLAN')
if stage=='CONTINUATION':base.fixture.settings.world_continuation_enabled=True
if not base.NO_PROVIDERS:
    def blocked(prompt,schema,session):
        if schema.__name__=='WorldContinuationDecision':
            if session.endswith(':2'):
                entered.set()
                if not release.wait(10):raise RuntimeError('local continuation gate timeout')
            with base.Path(os.environ['MODEL_BUDGET_CALL_LOG']).open('a',encoding='utf-8') as stream:
                stream.write(base.json.dumps({'pid':os.getpid(),'schema':schema.__name__,'external_calls':0})+'\n')
            return (schema(action='STOP',keep=True,summary='Actual local read',personal_note='A useful detail')
                    if session.endswith(':2') else schema(action='CALL',capability='WEB_SEARCH',arguments={'query':'bounded reading'}))
        target=('PersonalWorldReadPlan','PersonalBrowsePlan') if stage=='PLAN' else ('PersonalBrowseAppraisal','RssBatchAppraisal')
        if schema.__name__ in target:
            entered.set()
            if not release.wait(10):raise RuntimeError('local fixture gate timeout')
        return original(prompt,schema,session)
    base.fixture.model.structured_for_session=blocked

@app.get('/__e2e/cancel-state')
def state():
    if not entered.is_set() and not base.NO_PROVIDERS:
        return {'entered':False}
    result=base.runtime()
    paths=['src/character_memory/runtime/capability_execution.py','src/character_memory/world_continuation.py','src/character_memory/world_web.py']
    result['source_sha256'].update({p:base.hashlib.sha256((base.ROOT/p).read_bytes()).hexdigest() for p in paths})
    result.update(entered=entered.is_set(),opportunities=[dict(x) for x in base.fixture.store.conn.execute('SELECT opportunity_id,character_id,phase FROM world_browse_decisions')],
        cancellations=[dict(x) for x in base.fixture.store.conn.execute('SELECT * FROM world_browse_cancellations')])
    return result

@app.post('/__e2e/cancel-release')
def unblock():
    release.set();return {'released':True}


