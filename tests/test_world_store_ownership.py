import pytest
from character_memory.api import create_api
from character_memory.storage.sqlite import SQLiteStore
from test_world_activity import FakeModel, FakeObserver, NOW


def test_lazy_runtime_reuses_route_store_and_world_commit(tmp_path, monkeypatch):
    import character_memory.app as app_module
    model=FakeModel();model.close=lambda: None
    monkeypatch.setattr(app_module, 'build_model', lambda settings: model)
    config=tmp_path/'config.yaml'
    config.write_text("api_key: isolated-mock\nembedding_provider: deterministic\nembedding_model: deterministic\n"+f"db_path: '{(tmp_path/'isolated.db').as_posix()}'\n"+"persona_path: personas/rin/persona.yaml\n",encoding='utf-8')
    app=create_api(str(config));access=app.state.character_memory
    from character_memory.world_web import attach_world_routes
    if access.world_activity_scheduler is None:attach_world_routes(app)
    try:
        bundle=access.require_bundle()
        assert bundle.store is access.read_store
        access.services.world_observer=FakeObserver()
        result=access.world_activity_scheduler.service.browse_character('rin',now=NOW,opportunity_id='lazy-real-path')
        assert result['kept'] and result['source_event_id']
        assert access.world_activity_scheduler.repository.get_browse_decision('lazy-real-path')['phase']=='APPLIED'
    finally:
        bundle.close()
        access.read_store.close()
        access.services.close()


def test_failed_commit_rolls_back_and_leaves_connection_usable(tmp_path):
    store=SQLiteStore(tmp_path/'commit.db')
    connection=store.conn
    class FailOnce:
        failed=False
        def __getattr__(self,name):return getattr(connection,name)
        def commit(self):
            if not self.failed:
                self.failed=True
                raise RuntimeError('injected commit failure')
            return connection.commit()
    store.conn=FailOnce()
    try:
        with pytest.raises(RuntimeError,match='injected commit failure'):
            with store.transaction(immediate=True):
                store.conn.execute("INSERT INTO mental_states(character_id,content,updated_at) VALUES('probe','must rollback','now')")
        assert not connection.in_transaction and store._tx_depth==0
        assert connection.execute("SELECT count(*) FROM mental_states WHERE character_id='probe'").fetchone()[0]==0
        with store.transaction(immediate=True):
            store.conn.execute("INSERT INTO mental_states(character_id,content,updated_at) VALUES('probe','valid','now')")
    finally:store.close()
