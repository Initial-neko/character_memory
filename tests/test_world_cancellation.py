"""Durable cancellation fences later admissions and local effects, never paid facts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import threading

from pydantic import BaseModel
import pytest

from character_memory.storage.sqlite import SQLiteStore
from character_memory.world_activity import WorldPulseRepository
from character_memory.world_recovery import WorldBrowseRecovery
from test_world_execution_integration import setup, NOW
from test_rss_world_perception import consumer_fixture


def rows(store, table):
    exists = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] if exists else 0


def assert_cancelled(operation):
    with pytest.raises(ValueError) as error:
        operation()
    assert type(error.value).__name__ == 'WorldBrowseCancelled'


@pytest.mark.parametrize('kind', ['WEB', 'RSS'])
@pytest.mark.parametrize('stage', ['PLAN', 'APPRAISAL'])
def test_cancel_during_paid_model_keeps_receipt_but_blocks_later_effects(tmp_path, kind, stage):
    if kind == 'RSS':
        store, access, model, _, _, _, service = consumer_fixture(tmp_path)
        repo = service.repository
    else:
        store, access, model, repo, service = setup(tmp_path)
    other = SQLiteStore(store.path)
    cancel_repo = WorldPulseRepository(other)
    entered, release = threading.Event(), threading.Event()
    original = model.structured_for_session
    target = ('PersonalWorldReadPlan' if kind == 'RSS' else 'PersonalBrowsePlan') if stage == 'PLAN' else ('RssBatchAppraisal' if kind == 'RSS' else 'PersonalBrowseAppraisal')
    def blocked(prompt, schema, session):
        if schema.__name__ == target:
            entered.set()
            assert release.wait(8), 'test did not release admitted provider'
        return original(prompt, schema, session)
    model.structured_for_session = blocked
    opportunity = kind + ':' + stage
    try:
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(service.browse_character, 'c00', now=NOW, opportunity_id=opportunity)
            assert entered.wait(5)
            try:
                cancelled = cancel_repo.cancel_browse(opportunity, 'c00')
                assert cancelled['status'] == 'CANCELLED' and cancelled['cancelled'] is True
                assert cancelled['requested_at']
            finally:
                release.set()
            assert_cancelled(running.result)
        expected_calls = 1 if stage == 'PLAN' else 2
        assert len(model.calls) == expected_calls
        assert rows(store, 'model_invocation_receipts') == expected_calls
        assert {r[0] for r in store.conn.execute('SELECT status FROM model_invocation_receipts')} == {'SUCCESS'}
        assert rows(store, 'capability_executions') == int(stage == 'APPRAISAL')
        assert repo.get_browse_decision(opportunity)['phase'] == ('PLANNING' if stage == 'PLAN' else 'APPRAISING')
        assert not store.list_events('c00')
        if kind == 'RSS' and stage == 'APPRAISAL':
            assert {r[0] for r in store.conn.execute('SELECT status FROM world_rss_readings')} == {'STARTED'}
        assert cancel_repo.cancel_browse(opportunity, 'c00') == cancelled
        assert_cancelled(lambda: service.browse_character('c00', now=NOW + timedelta(days=2), opportunity_id=opportunity))
        assert len(model.calls) == expected_calls
        assert not store.list_events('c00')
    finally:
        release.set()
        other.close()
        store.close()


def test_cancellation_is_stable_across_connections_and_restart_without_phase_rewrite(tmp_path):
    path = tmp_path / 'cancel.sqlite'
    store = SQLiteStore(path)
    repo = WorldPulseRepository(store)
    repo.claim_browse_decision('owned', 'c00', NOW)
    receipt = repo.cancel_browse('owned', 'c00')
    assert receipt['status'] == 'CANCELLED' and receipt['cancelled'] is True
    store.close()
    reopened = SQLiteStore(path)
    try:
        repo = WorldPulseRepository(reopened)
        assert repo.cancel_browse('owned', 'c00') == receipt
        assert repo.get_browse_decision('owned')['phase'] == 'PLANNING'
        assert_cancelled(lambda: repo.assert_browse_active('c00', 'owned'))
        with pytest.raises(KeyError):
            repo.cancel_browse('owned', 'another-character')
        with pytest.raises(KeyError):
            repo.cancel_browse('unknown', 'c00')
    finally:
        reopened.close()


def test_applied_winner_cannot_be_cancelled_or_have_fact_deleted(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    try:
        result = service.browse_character('c00', now=NOW, opportunity_id='already-applied')
        event = store.get_event(result['source_event_id'])
        receipt = repo.cancel_browse('already-applied', 'c00')
        assert receipt['status'] == 'ALREADY_APPLIED' and receipt['cancelled'] is False
        assert store.get_event(event.id) == event
        assert service.browse_character('c00', now=NOW, opportunity_id='already-applied') == result
        assert len(model.calls) == 2
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['WEB', 'RSS'])
def test_cancelled_appraising_is_read_only_inspectable_and_not_recoverable(tmp_path, kind):
    from test_appraising_recovery import interrupted_appraisal
    store, _, model, service, opportunity = interrupted_appraisal(tmp_path, kind)
    try:
        service.repository.cancel_browse(opportunity, 'c00')
        changes = store.conn.total_changes
        report = WorldBrowseRecovery(store).inspect()
        assert store.conn.total_changes == changes
        item = next(item for item in report['candidates'] if item['opportunity_id'] == opportunity)
        assert not item['recoverable'] and 'CANCEL' in item['reason']
        assert_cancelled(lambda: WorldBrowseRecovery(store).apply(opportunity))
        assert not store.list_events('c00') and len(model.calls) == 2
        assert service.repository.get_browse_decision(opportunity)['phase'] == 'APPRAISING'
    finally:
        store.close()


def test_cancel_and_explicit_apply_have_one_atomic_winner(tmp_path):
    from test_appraising_recovery import interrupted_appraisal
    store, _, model, service, opportunity = interrupted_appraisal(tmp_path)
    other = SQLiteStore(store.path)
    repo = WorldPulseRepository(other)
    gate = threading.Barrier(2)
    def apply():
        gate.wait()
        try:
            return WorldBrowseRecovery(store).apply(opportunity)
        except ValueError as error:
            assert type(error).__name__ == 'WorldBrowseCancelled'
            return None
    def cancel():
        gate.wait()
        return repo.cancel_browse(opportunity, 'c00')
    try:
        with ThreadPoolExecutor(2) as pool:
            pending_apply = pool.submit(apply)
            pending_cancel = pool.submit(cancel)
            result, cancellation = pending_apply.result(), pending_cancel.result()
        if cancellation['cancelled']:
            assert result is None and not store.list_events('c00')
            assert service.repository.get_browse_decision(opportunity)['phase'] == 'APPRAISING'
        else:
            assert cancellation['status'] == 'ALREADY_APPLIED'
            assert result['kept'] and len(store.list_events('c00')) == 1
        assert len(model.calls) == 2
    finally:
        other.close()
        store.close()


def test_model_guard_runs_inside_claim_transaction_and_before_cached_return(tmp_path):
    from character_memory.runtime.model_execution import ModelInvocationBudget
    class Answer(BaseModel):
        text: str
    store = SQLiteStore(tmp_path / 'guard.sqlite')
    repo = WorldPulseRepository(store)
    repo.claim_browse_decision('model-guard', 'c00', NOW)
    budget = ModelInvocationBudget(store, daily_max=3)
    seen, calls = [], []
    def guard(character, opportunity):
        assert store.conn.in_transaction and store._tx_depth >= 1
        seen.append((character, opportunity))
        repo.assert_browse_active(character, opportunity)
    args = dict(character_id='c00', opportunity_id='model-guard', step='PLAN', identity={'hash': 'trusted'}, schema=Answer,
                call=lambda: calls.append(1) or Answer(text='accepted'), admission_guard=guard)
    try:
        assert budget.invoke(**args).text == 'accepted'
        repo.cancel_browse('model-guard', 'c00')
        assert_cancelled(lambda: budget.invoke(**args))
        assert_cancelled(lambda: budget.invoke(**{**args, 'step': 'APPRAISE'}))
        assert len(seen) == 3 and calls == [1]
        assert rows(store, 'model_invocation_receipts') == 1
    finally:
        store.close()


def test_tool_guard_runs_before_cached_or_new_claim_in_transaction(tmp_path):
    from dataclasses import replace
    from character_memory.runtime.capability_execution import CapabilityExecutor
    from test_capability_execution import request, Observer
    store = SQLiteStore(tmp_path / 'tool-guard.sqlite')
    repo = WorldPulseRepository(store)
    req = request()
    repo.claim_browse_decision(req.opportunity_id, req.character_id, NOW)
    observer = Observer()
    seen = []
    def guard(character, opportunity):
        assert store.conn.in_transaction and store._tx_depth >= 1
        seen.append((character, opportunity))
        repo.assert_browse_active(character, opportunity)
    try:
        executor = CapabilityExecutor(store, observer, admission_guard=guard)
        assert executor.execute(req).status == 'SUCCESS'
        repo.cancel_browse(req.opportunity_id, req.character_id)
        assert_cancelled(lambda: executor.execute(req))
        assert_cancelled(lambda: executor.execute(replace(req, request_id='another-tool')))
        assert len(seen) == 3 and len(observer.calls) == 1
        assert rows(store, 'capability_executions') == 1
    finally:
        store.close()
