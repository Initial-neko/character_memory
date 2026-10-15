from datetime import datetime, timezone

import pytest

from character_memory.ensemble_builder import EnsembleBuilderService, EnsembleRepository
from character_memory.group_scenes import GroupScenes
from character_memory.group_store import GroupRepository
from test_ensemble_builder import _access


def scene(tmp_path):
    access,store,_,_,_= _access(tmp_path)
    service=EnsembleBuilderService(access,EnsembleRepository(store))
    build=service.prepare_template('warm-isekai')
    active=service.confirm(build['group_id'],[0,1,2,3])
    return access,store,service,active['group_id']


def test_scene_day_is_an_explicit_idempotent_fact_and_resumes(tmp_path):
    _,store,_,identity=scene(tmp_path)
    scenes=GroupScenes(store)
    assert scenes.state(identity)['story_day']==1
    state=scenes.advance(identity,expected_day=1,request_id='advance-1')
    assert state['story_day']==2 and state['phase']=='MORNING'
    assert scenes.advance(identity,expected_day=1,request_id='advance-1')['story_day']==2
    with pytest.raises(ValueError): scenes.advance(identity,expected_day=1,request_id='advance-2')
    assert scenes.state(identity,max_event_id=state['last_control_event_id']-1)['story_day']==1
    assert not GroupRepository(store).list_event_page(identity).events
    assert store.conn.execute("SELECT count(*) FROM conversation_events WHERE event_type='SCENE_DAY_ADVANCED'").fetchone()[0]==1
    assert store.conn.execute('SELECT count(*) FROM memories').fetchone()[0]==0
    assert GroupScenes(store).resume('warm-isekai')['group_id']==identity
    store.close()


def test_other_ready_build_is_not_overwritten_by_scene_entry(tmp_path):
    access,store,_,_,_= _access(tmp_path)
    repo=EnsembleRepository(store)
    other=repo.create('other','保留我的其他群聊草稿','别的群',datetime.now(timezone.utc),mode='ORIGINAL')
    service=EnsembleBuilderService(access,repo)
    build=service.prepare_template('warm-isekai')
    assert GroupScenes(store).resume('warm-isekai')['group_id']==build['group_id']
    assert repo.get(other['group_id']) is not None
    normal=GroupRepository(store).create_group('普通群',['a','b'],datetime.now(timezone.utc))
    assert GroupScenes(store).state(normal.id) is None
    with pytest.raises(KeyError): GroupScenes(store).advance(normal.id,expected_day=1,request_id='no')
    store.close()


def test_scene_scheduler_never_claims_a_background_opportunity(tmp_path):
    import json
    from character_memory.application.group_autonomy import GroupAutonomyScheduler
    from test_group_autonomy import _access as autonomy_access, _group
    access, store, model, _ = autonomy_access(tmp_path)
    now = access.require_bundle().clock.now()
    group = _group(store, ['c00', 'c01'], now)
    repo = EnsembleRepository(store)
    repo.create(group.id, 'scene', '灯溪镇', now, mode='ORIGINAL')
    with store.transaction():
        store.conn.execute("UPDATE ensemble_builds SET status='ACTIVE',drafts_json=? WHERE group_id=?",
                           (json.dumps([{'template_id':'warm-isekai'}]), group.id))
    scheduler = GroupAutonomyScheduler(access, GroupRepository(store))
    scheduler.force_due(group.id, now=now)
    assert scheduler.run_once(now) == []
    assert model.calls == []
    assert store.conn.execute('SELECT count(*) FROM group_autonomy_runs').fetchone()[0] == 0
    store.close()


def test_scene_context_uses_trigger_day_and_survives_database_reopen(tmp_path):
    from character_memory.application.clock import FixedClock
    from character_memory.application.group_conversation_service import GroupConversationService
    from character_memory.domain.models import PersonReaction
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.storage.sqlite import SQLiteStore
    from test_p0_11_group_conversation import SequenceModel, runtime_for
    _, store, _, identity = scene(tmp_path)
    group = GroupRepository(store).get_group(identity)
    embeddings = DeterministicEmbedding()
    model = SequenceModel([PersonReaction(actions=[])] * 8)
    service = GroupConversationService(store, {
        member:runtime_for(store, embeddings, model, member) for member in group.member_ids
    }, FixedClock(datetime.now(timezone.utc)))
    old = service.persist_user_event(identity, '今晚好安静。')
    GroupScenes(store).advance(identity, expected_day=1, request_id='next')
    service.react_from_event(old)
    assert len(model.contexts) == 4
    assert all('故事第1天' in context for context in model.contexts)
    service.send(identity, '早上好。')
    assert all('故事第2天' in context for context in model.contexts[4:])
    path = store.path
    store.close()
    reopened = SQLiteStore(path)
    assert GroupScenes(reopened).resume('warm-isekai')['scene']['story_day'] == 2
    reopened.close()


def test_concurrent_scene_advances_only_one_day(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from character_memory.storage.sqlite import SQLiteStore
    _, store, _, identity = scene(tmp_path)
    second = SQLiteStore(store.path)
    def advance(pair):
        connection, request_id = pair
        try:
            return GroupScenes(connection).advance(identity, expected_day=1, request_id=request_id)['story_day']
        except ValueError:
            return 'CONFLICT'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, [(store, 'first'), (second, 'second')]))
    assert sorted(map(str, results)) == ['2', 'CONFLICT']
    assert GroupScenes(store).state(identity)['story_day'] == 2
    second.close()
    store.close()
