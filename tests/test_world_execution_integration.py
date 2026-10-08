from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event as Signal

import pytest

from character_memory.world_activity import WorldActivityService, WorldActivityScheduler, WorldPulseRepository
from test_world_activity import make_access, NOW


def setup(tmp_path):
    store, access, model = make_access(tmp_path, count=1)
    access.settings.world_pulse_enabled = False
    repo = WorldPulseRepository(store)
    return store, access, model, repo, WorldActivityService(access, repo)


def test_web_consumer_persists_real_execution_and_replays_without_model_or_browser(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    result = service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert result["execution_status"] == "SUCCESS" and result["kept"]
    assert len(model.calls) == 2
    event = store.get_event(result["source_event_id"])
    assert event.metadata["capability_request_id"] == result["capability_request_id"]
    assert event.metadata["opportunity_id"] == "test-op:1"
    assert event.metadata["observation_key"] == result["capability_request_id"]
    repeated = service.browse_character("c00", now=NOW + timedelta(hours=1), opportunity_id="test-op:1")
    assert repeated == result and len(model.calls) == 2
    assert len(store.list_events("c00")) == 1 and store.list_memories("c00") == []
    store.close()
    store, access, model, repo, service = setup(tmp_path)
    repeated = service.browse_character("c00", now=NOW + timedelta(hours=2), opportunity_id="test-op:1")
    assert repeated == result and model.calls == []
    store.close()


def test_no_action_is_one_original_decision_and_zero_execution(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    model.browse = False
    result = service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert not result["browsed"] and not result["kept"] and len(model.calls) == 1
    assert result["execution_status"] == "SKIPPED"
    assert not store.list_events("c00")
    assert service.browse_character("c00", now=NOW, opportunity_id="test-op:1") == result
    assert len(model.calls) == 1
    store.close()


def test_appraised_local_commit_can_resume_after_failure_without_paying_again(tmp_path):
    import sqlite3
    store, access, model, repo, service = setup(tmp_path)
    store.conn.execute("CREATE TRIGGER reject_observation BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'event write failed'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
        service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert len(model.calls) == 2 and not store.list_events("c00")
    assert repo.get_browse_decision("test-op:1")["phase"] == "APPRAISED"
    store.conn.execute("DROP TRIGGER reject_observation")
    store.conn.commit()
    result = service.browse_character("c00", now=NOW + timedelta(hours=1), opportunity_id="test-op:1")
    assert result["kept"] and len(model.calls) == 2
    assert store.get_event(result["source_event_id"]).event_time == NOW
    assert len(store.list_events("c00")) == 1
    store.close()


@pytest.mark.parametrize("phase", ["PLANNING", "APPRAISING"])
def test_unknown_model_outcome_is_not_replayed_after_restart(tmp_path, phase):
    store, access, model, repo, service = setup(tmp_path)
    if phase == "PLANNING":
        repo.claim_browse_decision("test-op:1", "c00", NOW)
    else:
        def crash(prompt, schema, session_id):
            if schema.__name__ == "PersonalBrowseAppraisal":
                raise KeyboardInterrupt("simulate process death")
            return original(prompt, schema, session_id)
        original = model.structured_for_session
        model.structured_for_session = crash
        with pytest.raises(KeyboardInterrupt):
            service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    store.close()
    store, access, model, repo, service = setup(tmp_path)
    result = service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert result["opportunity_status"] == "UNKNOWN" and not result["kept"]
    assert model.calls == [] and not store.list_events("c00")
    store.close()


def test_execution_failure_never_calls_appraisal_or_creates_personal_observation(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("browser offline")
    access.world_observer.observe = fail
    with pytest.raises(RuntimeError, match="browser offline"):
        service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert [name for name, _, _ in model.calls] == ["PersonalBrowsePlan"]
    row = store.conn.execute("SELECT status FROM capability_executions").fetchone()
    assert row["status"] == "FAILED"
    assert not store.list_events("c00")
    repeated = service.browse_character("c00", now=NOW, opportunity_id="test-op:1")
    assert not repeated["kept"] and repeated["execution_status"] == "FAILED" and len(model.calls) == 1
    store.close()


def test_manual_calls_in_same_minute_have_distinct_durable_opportunities(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    first = service.browse_character("c00", now=NOW)
    second = service.browse_character("c00", now=NOW)
    assert first["opportunity_id"] != second["opportunity_id"]
    assert first["capability_request_id"] != second["capability_request_id"]
    assert len(model.calls) == 4 and len(store.list_events("c00")) == 2
    assert repo.count_runs_since("BROWSE", "c00", NOW) == 0
    store.close()


def test_saving_mode_doubles_only_background_browse_and_rearms_without_catchup(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    scheduler = WorldActivityScheduler(access, repo)
    assert access.settings.world_cost_saving_enabled is False
    assert scheduler._interval("world_browse_interval_minutes", 30) == 90
    pulse_before = scheduler._interval("world_pulse_refresh_minutes", 60)
    scheduler.run_once(now=NOW)
    old_recheck = scheduler.browse_idle_recheck_minutes()
    access.settings.world_cost_saving_enabled = True
    later = NOW + timedelta(minutes=5)
    scheduler.run_once(now=later)
    state = next(row for row in repo.states() if row["kind"] == "BROWSE")
    assert state["configured_interval_minutes"] == 180
    assert state["next_run_at_epoch"] > int(later.timestamp() * 1000000)
    assert scheduler._interval("world_pulse_refresh_minutes", 60) == pulse_before
    assert scheduler.browse_idle_recheck_minutes() >= old_recheck
    access.settings.world_cost_saving_enabled = False
    scheduler.run_once(now=later + timedelta(minutes=1))
    state = next(row for row in repo.states() if row["kind"] == "BROWSE")
    assert state["configured_interval_minutes"] == 90 and model.calls == []
    # Explicit manual browsing uses original page limits and two calls.
    service.browse_character("c00", now=later)
    assert len(model.calls) == 2
    store.close()


def test_two_schedulers_claim_same_due_opportunity_only_once(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    second_store, second_access, second_model, second_repo, _ = setup(tmp_path)
    repo.ensure_state("BROWSE", "c00", NOW, delay_minutes=0, interval_minutes=90)
    first_scheduler = WorldActivityScheduler(access, repo)
    second_scheduler = WorldActivityScheduler(second_access, second_repo)
    entered = Signal()
    release = Signal()
    original = model.structured_for_session
    def blocked(*args):
        if args[1].__name__ == "PersonalBrowsePlan":
            entered.set()
            assert release.wait(2)
        return original(*args)
    model.structured_for_session = blocked
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_scheduler.run_once, now=NOW)
            assert entered.wait(2)
            second = pool.submit(second_scheduler.run_once, now=NOW).result()
            release.set()
            first.result()
        assert len(model.calls) == 2 and second_model.calls == []
        assert repo.count_runs_since("BROWSE", "c00", NOW) == 1
    finally:
        store.close()
        second_store.close()


def test_kept_false_preserves_execution_fact_without_personal_experience(tmp_path):
    from character_memory.world_activity import PersonalBrowseAppraisal
    store, access, model, repo, service = setup(tmp_path)
    original = model.structured_for_session
    def ignore(*args):
        value = original(*args)
        return PersonalBrowseAppraisal(keep=False, summary="irrelevant") if args[1] is PersonalBrowseAppraisal else value
    model.structured_for_session = ignore
    result = service.browse_character("c00", now=NOW, opportunity_id="test-op:ignored")
    assert result["execution_status"] == "SUCCESS" and result["appraisal_status"] == "IGNORED"
    assert result["source_event_id"] is None and not store.list_events("c00")
    assert len(model.calls) == 2 and store.list_memories("c00") == []
    assert repo.get_browse_decision("test-op:ignored")["phase"] == "APPLIED"
    assert store.conn.execute("SELECT status FROM capability_executions").fetchone()[0] == "SUCCESS"
    store.close()


def test_opportunity_identity_cannot_expose_another_persons_cached_result(tmp_path):
    store, access, model = make_access(tmp_path, count=2)
    repo = WorldPulseRepository(store)
    service = WorldActivityService(access, repo)
    service.browse_character("c00", now=NOW, opportunity_id="private-op")
    with pytest.raises(ValueError, match="another character"):
        service.browse_character("c01", now=NOW, opportunity_id="private-op")
    assert len(model.calls) == 2 and not store.list_events("c01")
    store.close()


def test_saved_plan_keeps_original_execution_limits_after_restart(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    # An interrupted external execution is not called again, even after changing
    # configuration. Its request still matches the original persisted plan.
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("provider outcome unknown")
    access.world_observer.observe = interrupt
    with pytest.raises(KeyboardInterrupt):
        service.browse_character("c00", now=NOW, opportunity_id="test-op:interrupted")
    store.close()
    store, access, model, repo, service = setup(tmp_path)
    access.settings.world_browse_max_pages = 4
    access.settings.space_world_max_chars_per_page = 16000
    result = service.browse_character("c00", now=NOW, opportunity_id="test-op:interrupted")
    assert result["opportunity_status"] == "UNKNOWN"
    assert model.calls == [] and not store.list_events("c00")
    assert store.conn.execute("SELECT status FROM capability_executions").fetchone()[0] == "STARTED"
    store.close()
