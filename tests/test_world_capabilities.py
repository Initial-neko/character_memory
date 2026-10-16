from types import SimpleNamespace

import pytest

from character_memory.world_activity import WorldActivityService, WorldPulseRepository
from character_memory.world_observation import WorldObservationService
from test_world_activity import make_access, NOW
from test_claude_world import _grant_role, Runner


def setup(tmp_path):
    store, access, model = make_access(tmp_path, count=1)
    service = WorldActivityService(access, WorldPulseRepository(store))
    return store, access, model, service


def test_world_snapshot_uses_current_services_and_is_read_only(tmp_path):
    store, access, model, service = setup(tmp_path)
    before = store.conn.total_changes
    assert [c.id for c in service.capabilities('c00', now=NOW)] == ['WEB_SEARCH']
    assert store.conn.total_changes == before and not model.calls
    access.world_observer = WorldObservationService(None, access.world_fetcher)
    assert service.capabilities('c00', now=NOW) == ()
    assert service.capabilities('missing', now=NOW) == ()
    store.close()


def test_world_rss_visibility_requires_enabled_candidates(tmp_path):
    store, access, model, service = setup(tmp_path)
    candidates = [{'item_id':1, 'source_generation':0}]
    assert [c.id for c in service.capabilities('c00', now=NOW, rss_candidates=candidates)] == ['WEB_SEARCH']
    access.settings.world_rss_reading_enabled = True
    assert [c.id for c in service.capabilities('c00', now=NOW, rss_candidates=candidates)] == ['WEB_SEARCH','READ_RSS']
    assert [c.id for c in service.capabilities('c00', now=NOW)] == ['WEB_SEARCH']
    assert service.capabilities('c00', now=NOW, remaining_calls=0) == ()
    store.close()


def test_world_claude_authorization_and_readiness_share_discovery(tmp_path):
    store, access, model, service = setup(tmp_path)
    access.settings.world_browse_execution_backend = 'claude'
    runner = Runner()
    access.services = SimpleNamespace(claude_runner=runner)
    assert service.capabilities('c00', now=NOW)[0].backend == 'direct'
    _grant_role(tmp_path, access, 'c00')
    assert service.capabilities('c00', now=NOW)[0].backend == 'claude'
    access.services.claude_runner = None
    assert service.capabilities('c00', now=NOW) == ()
    access.settings.claude_execution_daily_max = 0
    access.services.claude_runner = runner
    assert service.capabilities('c00', now=NOW) == ()
    assert runner.calls == []
    store.close()


def test_model_cannot_execute_missing_web_capability(tmp_path):
    store, access, model, service = setup(tmp_path)
    access.world_observer = WorldObservationService(None, access.world_fetcher)
    with pytest.raises(ValueError, match='CAPABILITY_UNAVAILABLE'):
        service.browse_character('c00', now=NOW, opportunity_id='denied-web')
    assert len(model.calls) == 1
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 0
    assert store.list_events('c00') == []
    store.close()
