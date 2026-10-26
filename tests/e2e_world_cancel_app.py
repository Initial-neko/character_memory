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
if not base.NO_PROVIDERS:
    def blocked(prompt,schema,session):
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

