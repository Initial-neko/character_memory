import pytest
from character_memory.world_recovery import WorldBrowseRecovery
from test_rss_world_perception import consumer_fixture
from test_world_activity import NOW


def interrupted(tmp_path,monkeypatch):
    import character_memory.world_activity as world
    store,access,model,_,_,reading,service=consumer_fixture(tmp_path)
    keep=world.retain_world_observation
    monkeypatch.setattr(world,'retain_world_observation',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('commit interrupted')))
    with pytest.raises(RuntimeError,match='commit interrupted'):
        service.browse_character('c00',now=NOW,opportunity_id='recover-rss')
    monkeypatch.setattr(world,'retain_world_observation',keep)
    return store,access,model,reading,service


def test_rss_recovery_inspection_is_read_only_and_apply_buys_nothing(tmp_path,monkeypatch):
    store,access,model,reading,service=interrupted(tmp_path,monkeypatch)
    try:
        recovery=WorldBrowseRecovery(store);changes=store.conn.total_changes
        report=recovery.inspect()
        assert report['read_only'] and report['candidates'][0]['recoverable']
        assert store.conn.total_changes==changes
        calls=len(model.calls)
        access.require_bundle=lambda: pytest.fail('recovery must not initialize model')
        result=service.browse_character('c00',now=NOW,opportunity_id='recover-rss')
        assert result['kept'] and len(model.calls)==calls
        assert len(store.list_events('c00'))==2
        assert recovery.apply('recover-rss')==result
        assert len(store.list_events('c00'))==2
    finally:store.close()


def test_bad_receipt_is_reported_without_partial_write(tmp_path,monkeypatch):
    store,_,_,_,_=interrupted(tmp_path,monkeypatch)
    try:
        store.conn.execute("UPDATE capability_executions SET request_json='{}'");store.conn.commit()
        recovery=WorldBrowseRecovery(store);changes=store.conn.total_changes
        assert not recovery.inspect()['candidates'][0]['recoverable']
        with pytest.raises(ValueError,match='INVALID_RECOVERY_RECEIPT'):recovery.apply('recover-rss')
        assert store.conn.total_changes==changes
        assert store.list_events('c00')==[]
        assert not store.conn.in_transaction
    finally:store.close()


def test_web_recovery_uses_saved_reading_and_appraisal(tmp_path,monkeypatch):
    import character_memory.world_activity as world
    from test_world_activity import make_access
    store,access,model=make_access(tmp_path)
    repository=world.WorldPulseRepository(store);service=world.WorldActivityService(access,repository)
    original=world.retain_world_observation
    monkeypatch.setattr(world,'retain_world_observation',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('local failed')))
    try:
        with pytest.raises(RuntimeError,match='local failed'):
            service.browse_character('c00',now=NOW,opportunity_id='web-recover')
        monkeypatch.setattr(world,'retain_world_observation',original)
        access.require_bundle=lambda: pytest.fail('recovery cannot initialize model')
        calls=len(model.calls)
        result=WorldBrowseRecovery(store).apply('web-recover')
        assert result['kept'] and result['source_event_id']
        assert len(model.calls)==calls and len(store.list_events('c00'))==1
    finally:store.close()
