from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.world_observation import retain_world_observation

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def observation():
    return Event(character_id="a", event_type=EventType.WORLD_OBSERVATION,
                 event_time=NOW, content="这次阅读让我想继续关注长期记忆。",
                 metadata={"channel": "WORLD", "sources": ["https://example.org/article"]})


def test_real_observation_without_cognition_is_valid_and_restart_idempotent(tmp_path):
    path = tmp_path / "local.sqlite"
    store = SQLiteStore(path)
    first, created = retain_world_observation(store, observation(), observation_key="read-1")
    assert created
    life = first.metadata["observation_lifecycle"]
    assert life["reading_scope"] == "WEB_PAGES"
    assert life["reading_status"] == "SUCCEEDED"
    assert life["appraisal_status"] == "KEPT"
    assert life["cognition_status"] == "NOT_REQUESTED"
    assert life["memory_status"] == "NOT_REQUESTED"
    assert life["observed_at"] == NOW.isoformat()
    assert first.metadata["sources"] == ["https://example.org/article"]
    assert store.list_memories("a") == []
    store.close()
    store = SQLiteStore(path)
    repeated, created = retain_world_observation(store, observation(), observation_key="read-1")
    assert not created and repeated.id == first.id
    assert len(store.list_events("a")) == 1
    assert "world/003-observation-lifecycle" in store.list_schema_migrations()
    store.close()


def test_duplicate_result_does_not_repeat_cognition_or_derived_effects(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    calls = []
    runtime = SimpleNamespace(handle=lambda event, **kwargs: calls.append((event.id, kwargs)) or
                              SimpleNamespace(created_memory_ids=[12]))
    first, created = retain_world_observation(store, observation(), observation_key="read-1", runtime=runtime)
    repeated, again = retain_world_observation(store, observation(), observation_key="read-1", runtime=runtime)
    assert created and not again and first.id == repeated.id
    assert calls == [(first.id, {"persist_event": False})]
    assert repeated.metadata["observation_lifecycle"]["cognition_status"] == "COMPLETED"
    assert repeated.metadata["observation_lifecycle"]["created_memory_ids"] == [12]
    store.close()


def test_cognition_failure_keeps_true_reading_and_does_not_auto_replay(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    calls = []
    def fail(event, **kwargs):
        calls.append(event.id)
        raise RuntimeError("provider unavailable")
    runtime = SimpleNamespace(handle=fail)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        retain_world_observation(store, observation(), observation_key="read-1", runtime=runtime)
    event = store.list_events("a")[0]
    assert event.metadata["observation_lifecycle"]["cognition_status"] == "FAILED"
    assert store.list_memories("a") == []
    repeated, created = retain_world_observation(store, observation(), observation_key="read-1", runtime=runtime)
    assert not created and repeated.id == event.id and calls == [event.id]
    store.close()


def test_key_collision_rejects_different_result_and_non_observation(tmp_path):
    store = SQLiteStore(tmp_path / "local.sqlite")
    retain_world_observation(store, observation(), observation_key="read-1")
    with pytest.raises(ValueError, match="different"):
        retain_world_observation(store, observation().model_copy(update={"content": "different"}), observation_key="read-1")
    with pytest.raises(ValueError):
        retain_world_observation(store, observation().model_copy(update={"event_type": EventType.USER_MESSAGE}), observation_key="wrong")
    assert len(store.list_events("a")) == 1
    store.close()


def test_two_connections_commit_one_observation_and_legacy_is_untouched(tmp_path):
    path = tmp_path / "local.sqlite"
    seed = SQLiteStore(path)
    legacy = seed.append_event(observation())
    retain_world_observation(seed, observation(), observation_key="initialize")
    seed.close()
    def submit(_):
        store = SQLiteStore(path)
        try:
            event, created = retain_world_observation(store, observation(), observation_key="concurrent")
            return event.id, created
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0][0] == results[1][0]
    assert sum(created for _, created in results) == 1
    store = SQLiteStore(path)
    assert store.get_event(legacy.id).metadata == observation().metadata
    store.close()


def test_actual_space_cognition_commits_lifecycle_with_memory_and_trace(tmp_path):
    from test_space_autonomy import _access, WorldSpaceModel, FakeWorldObserver
    from character_memory.space_autonomy import SpaceAutonomyService
    from character_memory.space_store import SpaceRepository
    model = WorldSpaceModel()
    access, store, _ = _access(tmp_path, ids=("c00",), model=model)
    access.settings.space_world_observation_enabled = True
    access.world_observer = FakeWorldObserver()
    result = SpaceAutonomyService(access, SpaceRepository(store)).run_opportunity("c00", now=NOW, cascade=False)
    world = result["world"]
    event = store.get_event(world["source_event_id"])
    trace = store.get_runtime_trace(event.id)
    life = event.metadata["observation_lifecycle"]
    assert life["cognition_status"] == "COMPLETED"
    assert life["created_memory_ids"] == world["created_memory_ids"] == trace["created_memory_ids"]
    assert trace["event"]["metadata"]["observation_lifecycle"] == life
    assert all(store.get_memory(mid).source_event_id == event.id for mid in world["created_memory_ids"])
    repeated, created = retain_world_observation(store, event, observation_key=event.metadata["observation_key"],
                                               runtime=access.require_bundle().runtimes["c00"])
    assert not created and repeated.id == event.id
    assert len(store.list_memories("c00")) == len(world["created_memory_ids"])
    store.close()


def test_actual_cognition_rollback_preserves_observation_but_not_memory(tmp_path, monkeypatch):
    from test_space_autonomy import _access, WorldSpaceModel
    access, store, _ = _access(tmp_path, ids=("c00",), model=WorldSpaceModel())
    runtime = access.require_bundle().runtimes["c00"]
    def fail(*args, **kwargs):
        raise RuntimeError("trace write failed")
    monkeypatch.setattr(store, "add_runtime_trace", fail)
    event = observation().model_copy(update={"character_id": "c00"})
    with pytest.raises(RuntimeError, match="trace write failed"):
        retain_world_observation(store, event, observation_key="rollback", runtime=runtime)
    saved = store.list_events("c00")[0]
    assert saved.metadata["observation_lifecycle"]["cognition_status"] == "FAILED"
    assert store.list_memories("c00") == []
    assert store.get_runtime_trace(saved.id) is None
    store.close()


@pytest.mark.parametrize("keep", [True, False])
def test_personal_browse_uses_existing_two_calls_and_never_creates_memory(tmp_path, keep):
    from test_world_activity import make_access, FakeModel
    from character_memory.world_activity import PersonalBrowseAppraisal, WorldActivityService, WorldPulseRepository
    class Model(FakeModel):
        def structured_for_session(self, prompt, schema, session_id):
            result = super().structured_for_session(prompt, schema, session_id)
            if schema is PersonalBrowseAppraisal:
                return PersonalBrowseAppraisal(keep=keep, summary="安全摘要", personal_note="我想继续研究。")
            return result
    store, access, _ = make_access(tmp_path, count=1)
    model = Model()
    access.require_bundle().model = model
    result = WorldActivityService(access, WorldPulseRepository(store)).browse_character("c00", now=NOW)
    assert len(model.calls) == 2
    assert store.list_memories("c00") == []
    assert result["appraisal_status"] == ("KEPT" if keep else "IGNORED")
    events = [event for event in store.list_events("c00") if event.event_type == EventType.WORLD_OBSERVATION]
    assert len(events) == int(keep)
    if keep:
        assert result["observation_lifecycle"]["cognition_status"] == "NOT_REQUESTED"
        assert events[0].metadata["content_kind"] == "PERSONAL_NOTE"
    else:
        assert result["source_event_id"] is None and result["observation_lifecycle"] is None
    store.close()


@pytest.mark.parametrize("stage", ["observe", "appraisal"])
def test_personal_browse_provider_failure_has_no_success_observation(tmp_path, stage):
    from test_world_activity import make_access
    from character_memory.world_activity import PersonalBrowseAppraisal, WorldActivityService, WorldPulseRepository
    store, access, model = make_access(tmp_path, count=1)
    if stage == "observe":
        def fail(*args, **kwargs):
            raise RuntimeError("browser failed")
        access.world_observer.observe = fail
    else:
        original = model.structured_for_session
        def fail(prompt, schema, session_id):
            if schema is PersonalBrowseAppraisal:
                raise RuntimeError("appraisal failed")
            return original(prompt, schema, session_id)
        model.structured_for_session = fail
    with pytest.raises(RuntimeError):
        WorldActivityService(access, WorldPulseRepository(store)).browse_character("c00", now=NOW)
    assert store.list_events("c00") == [] and store.list_memories("c00") == []
    store.close()


def test_failure_after_atomic_cognition_commit_does_not_mark_it_failed(tmp_path):
    from test_space_autonomy import _access, WorldSpaceModel
    access, store, _ = _access(tmp_path, ids=("c00",), model=WorldSpaceModel())
    actual = access.require_bundle().runtimes["c00"]
    def handle(event, **kwargs):
        actual.handle(event, **kwargs)
        raise RuntimeError("post-commit delivery failed")
    event = observation().model_copy(update={"character_id": "c00"})
    with pytest.raises(RuntimeError, match="post-commit"):
        retain_world_observation(store, event, observation_key="committed", runtime=SimpleNamespace(handle=handle))
    saved = next(e for e in store.list_events("c00") if e.event_type == EventType.WORLD_OBSERVATION)
    assert saved.metadata["observation_lifecycle"]["cognition_status"] == "COMPLETED"
    assert saved.metadata["observation_lifecycle"]["created_memory_ids"]
    repeated, created = retain_world_observation(store, event, observation_key="committed", runtime=actual)
    assert not created and repeated.id == saved.id
    store.close()


def test_pulse_records_topic_encounter_without_claiming_full_page_read(tmp_path):
    from test_world_activity import make_access
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository, WorldPulseTopicDraft
    store, access, model = make_access(tmp_path, count=1)
    repo = WorldPulseRepository(store)
    topic = repo.upsert_topic(WorldPulseTopicDraft(title="公开话题", summary="话题摘要", source_indexes=[1]),
                             ["https://example.org/topic"], NOW)
    WorldActivityService(access, repo).discuss_topic(topic["id"], now=NOW, character_ids=["c00"])
    event = next(e for e in store.list_events("c00") if e.event_type == EventType.WORLD_OBSERVATION)
    assert event.metadata["observation_lifecycle"]["reading_scope"] == "WORLD_PULSE_TOPIC"
    assert event.metadata["observation_lifecycle"]["cognition_status"] == "NOT_REQUESTED"
    assert store.list_memories("c00") == []
    store.close()


def test_two_connections_first_migration_serializes_before_reading_marker(tmp_path):
    from threading import Event as Signal
    path = tmp_path / "cold.sqlite"
    first = SQLiteStore(path)
    second = SQLiteStore(path)
    first_read = Signal()
    second_read = Signal()
    original_first = first._migration_applied_locked
    original_second = second._migration_applied_locked
    def read_first(name):
        value = original_first(name)
        if name == "world/003-observation-lifecycle":
            first_read.set()
            second_read.wait(0.2)
        return value
    def read_second(name):
        value = original_second(name)
        if name == "world/003-observation-lifecycle":
            second_read.set()
        return value
    first._migration_applied_locked = read_first
    second._migration_applied_locked = read_second
    def submit(store):
        event, created = retain_world_observation(store, observation(), observation_key="cold")
        return event.id, created
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(submit, first)
            assert first_read.wait(2)
            second_result = pool.submit(submit, second)
            results = [first_result.result(), second_result.result()]
        assert results[0][0] == results[1][0] and sum(created for _, created in results) == 1
        assert second_read.is_set()
        assert len(first.list_events("a")) == 1
    finally:
        first.close()
        second.close()
