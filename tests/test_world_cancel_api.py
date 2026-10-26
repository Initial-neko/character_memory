from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from test_world_execution_integration import setup, NOW
from test_world_continuation import setup as continuation_setup
from character_memory.world_web import attach_world_routes
from character_memory.web_lifecycle import BackgroundServices


def test_cancel_api_is_identity_bound_and_strict(tmp_path):
    store,access,model,repo,service=setup(tmp_path)
    access.services=SimpleNamespace(world_observer=access.world_observer,world_fetcher=access.world_fetcher)
    app=FastAPI();app.state.character_memory=access;app.state.background_services=BackgroundServices();attach_world_routes(app)
    repo.claim_browse_decision('api-op','c00',NOW)
    try:
        with TestClient(app) as client:
            url='/v1/world/activity/cancel/api-op'
            assert client.post(url,json={'character_id':'other'}).status_code==404
            assert client.post(url,json={'character_id':'c00','reason':'arbitrary'}).status_code==422
            assert client.post(url,json={'character_id':3}).status_code==422
            first=client.post(url,json={'character_id':'c00'})
            assert first.status_code==200,first.text
            assert first.json()['status']=='CANCELLED'
            assert client.post(url,json={'character_id':'c00'}).json()==first.json()
            assert client.post('/v1/world/activity/cancel/missing',json={'character_id':'c00'}).status_code==404
    finally:store.close()


def test_continuation_late_stop_cannot_commit_after_cancel(tmp_path):
    store,access,model,prompts,service=continuation_setup(tmp_path,[
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'agent memory'}},
        {'action':'STOP','keep':True,'summary':'Actual read','personal_note':'Useful detail'}])
    entered=threading.Event();release=threading.Event();original=model.structured_for_session
    def block(prompt,schema,session):
        if session.endswith(':2'):
            entered.set();assert release.wait(5)
        return original(prompt,schema,session)
    model.structured_for_session=block
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(service.browse_character,'c00',now=NOW,opportunity_id='cont-cancel')
            assert entered.wait(3)
            try:service.repository.cancel_browse('cont-cancel','c00')
            finally:release.set()
            with pytest.raises(ValueError,match='WORLD_OPPORTUNITY_CANCELLED'):future.result()
        assert not store.list_events('c00')
        assert store.conn.execute("SELECT COUNT(*) FROM model_invocation_receipts WHERE status='SUCCESS'").fetchone()[0]==2
        with pytest.raises(ValueError,match='WORLD_OPPORTUNITY_CANCELLED'):
            service.browse_character('c00',now=NOW,opportunity_id='cont-cancel')
    finally:release.set();store.close()


@pytest.mark.parametrize('where',['TOOL_SUCCESS','RSS_APPRAISAL_FAILURE'])
def test_cancel_preserves_admitted_tool_and_failed_model_receipts(tmp_path,where):
    from test_rss_world_perception import consumer_fixture
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.world_activity import WorldPulseRepository
    if where=='RSS_APPRAISAL_FAILURE':store,access,model,_,_,reading,service=consumer_fixture(tmp_path)
    else:store,access,model,_,service=setup(tmp_path)
    other=SQLiteStore(store.path);repo=WorldPulseRepository(other)
    entered=threading.Event();release=threading.Event()
    if where=='TOOL_SUCCESS':
        original=access.world_observer.observe
        def block(*args,**kwargs):
            entered.set();assert release.wait(5);return original(*args,**kwargs)
        access.world_observer.observe=block
    else:
        original=model.structured_for_session
        def block(*args):
            if args[1].__name__=='RssBatchAppraisal':
                entered.set();assert release.wait(5);raise TimeoutError('fixed local timeout')
            return original(*args)
        model.structured_for_session=block
    try:
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(service.browse_character,'c00',now=NOW,opportunity_id='late-extra')
            assert entered.wait(3)
            try:repo.cancel_browse('late-extra','c00')
            finally:release.set()
            with pytest.raises(ValueError,match='WORLD_OPPORTUNITY_CANCELLED'):future.result()
        assert not store.list_events('c00')
        assert store.conn.execute('SELECT status FROM capability_executions').fetchone()[0]=='SUCCESS'
        phase=service.repository.get_browse_decision('late-extra')['phase']
        assert phase==('PLANNED' if where=='TOOL_SUCCESS' else 'APPRAISING')
        if where=='RSS_APPRAISAL_FAILURE':
            assert {x[0] for x in store.conn.execute('SELECT status FROM world_rss_readings')}=={'STARTED'}
            assert {x[0] for x in store.conn.execute('SELECT status FROM model_invocation_receipts')}=={'SUCCESS','FAILED'}
    finally:release.set();other.close();store.close()


def test_cancelled_backlog_is_excluded_before_auto_recovery_limit(tmp_path,monkeypatch):
    from test_world_recovery import interrupted
    from character_memory.world_recovery import WorldBrowseRecovery
    store,access,model,reading,service=interrupted(tmp_path,monkeypatch)
    try:
        for i in range(201):
            op=f'cancel-backlog-{i}'
            service.repository.claim_browse_decision(op,'c00',NOW)
            service.repository.update_browse_decision(op,expected_phase='PLANNING',phase='APPRAISED',appraisal={})
            service.repository.cancel_browse(op,'c00')
        report=WorldBrowseRecovery(store).inspect(limit=200,include_explicit=False)
        assert len(report['candidates'])==1 and report['candidates'][0]['recoverable']
        assert report['candidates'][0]['opportunity_id']=='recover-rss'
    finally:store.close()
