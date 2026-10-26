"""Recover only proven paid appraisals, with no provider or runtime replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3
import threading

import pytest

from character_memory.world_activity import WorldActivityScheduler
from character_memory.world_recovery import WorldBrowseRecovery
from test_world_execution_integration import setup, NOW
from test_rss_world_perception import consumer_fixture


def interrupted_appraisal(tmp_path, kind='WEB'):
    if kind == 'RSS':
        store, access, model, _, _, _, service = consumer_fixture(tmp_path)
    else:
        store, access, model, _, service = setup(tmp_path)
        observe = access.world_observer.observe
        def two_pages(*args, **kwargs):
            result = observe(*args, **kwargs)
            result['observations'].append(result['observations'][0].model_copy(update={'title': 'Second actual fixture page', 'url': 'https://example.com/second', 'content': 'Different observed page content'}))
            return result
        access.world_observer.observe = two_pages
    opportunity = 'appraising-' + kind.lower()
    store.conn.execute("CREATE TRIGGER reject_appraisal BEFORE UPDATE ON world_browse_decisions WHEN NEW.phase='APPRAISED' BEGIN SELECT RAISE(ABORT,'appraisal save interrupted'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='appraisal save interrupted'):
        service.browse_character('c00', now=NOW, opportunity_id=opportunity)
    assert store.conn.execute('SELECT phase FROM world_browse_decisions WHERE opportunity_id=?', (opportunity,)).fetchone()[0] == 'APPRAISING'
    assert store.conn.execute("SELECT COUNT(*) FROM model_invocation_receipts WHERE status='SUCCESS'").fetchone()[0] == 2
    assert not store.list_events('c00') and len(model.calls) == 2
    store.conn.execute('DROP TRIGGER reject_appraisal')
    store.conn.commit()
    return store, access, model, service, opportunity


@pytest.mark.parametrize('kind', ['WEB', 'RSS'])
def test_proven_appraising_receipt_is_explicitly_recoverable(tmp_path, kind):
    store, access, model, service, opportunity = interrupted_appraisal(tmp_path, kind)
    try:
        recovery = WorldBrowseRecovery(store)
        before = store.conn.total_changes
        report = recovery.inspect()
        assert len(report['candidates']) == 1
        candidate = report['candidates'][0]
        assert candidate['phase'] == 'APPRAISING' and candidate['requires_explicit'] is True
        assert candidate['recoverable'] is True
        assert store.conn.total_changes == before
        access.require_bundle = lambda: pytest.fail('local recovery initialized runtime')
        result = recovery.apply(opportunity)
        assert result['kept'] and len(model.calls) == 2
        events = store.list_events('c00')
        assert len(events) == (2 if kind == 'RSS' else 1)
        assert all(event.event_time == NOW for event in events)
        assert recovery.apply(opportunity) == result
        assert len(store.list_events('c00')) == len(events)
        assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 2
    finally:
        store.close()



def assert_rejected_without_effect(store, opportunity):
    recovery = WorldBrowseRecovery(store)
    before = store.conn.total_changes
    report = recovery.inspect()
    assert len(report['candidates']) == 1 and not report['candidates'][0]['recoverable']
    with pytest.raises(ValueError):
        recovery.apply(opportunity)
    assert store.conn.total_changes == before
    assert not store.list_events('c00')
    assert store.conn.execute('SELECT phase FROM world_browse_decisions').fetchone()[0] == 'APPRAISING'


@pytest.mark.parametrize('mutation', ['persona', 'query', 'page-order', 'page-content'])
def test_changed_web_prompt_inputs_cannot_apply_saved_result(tmp_path, mutation):
    store, _, _, _, opportunity = interrupted_appraisal(tmp_path)
    try:
        if mutation in {'persona', 'query'}:
            row = store.conn.execute('SELECT plan_json FROM world_browse_decisions').fetchone()
            plan = json.loads(row[0])
            if mutation == 'persona':
                plan['persona'] += '\nChanged persona'
            else:
                plan['decision']['query'] = 'changed query'
            store.conn.execute('UPDATE world_browse_decisions SET plan_json=?', (json.dumps(plan),))
        else:
            row = store.conn.execute('SELECT result_json FROM capability_executions').fetchone()
            result = json.loads(row[0])
            if mutation == 'page-order':
                assert len(result['data']['observations']) == 2
                result['data']['observations'].reverse()
            else:
                result['data']['observations'][0]['content'] += ' changed body'
            store.conn.execute('UPDATE capability_executions SET result_json=?', (json.dumps(result),))
        store.conn.commit()
        assert_rejected_without_effect(store, opportunity)
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['WEB', 'RSS'])
@pytest.mark.parametrize('mutation', ['missing-binding', 'binding-policy', 'binding-model', 'binding-schema', 'result', 'request-hash', 'FAILED', 'STARTED'])
def test_missing_or_changed_model_proof_cannot_promote_appraising(tmp_path, kind, mutation):
    store, _, _, _, opportunity = interrupted_appraisal(tmp_path, kind)
    step = 'WEB_APPRAISAL' if kind == 'WEB' else 'RSS_APPRAISAL'
    try:
        row = store.conn.execute('SELECT * FROM model_invocation_receipts WHERE step=?', (step,)).fetchone()
        assert row is not None
        if mutation == 'missing-binding':
            store.conn.execute('UPDATE model_invocation_receipts SET binding_json=NULL WHERE step=?', (step,))
        elif mutation.startswith('binding-'):
            binding = json.loads(row['binding_json'])
            key = {'binding-policy': 'policy', 'binding-model': 'model_sha256', 'binding-schema': 'schema_sha256'}[mutation]
            binding[key] = '0' * 64
            store.conn.execute('UPDATE model_invocation_receipts SET binding_json=? WHERE step=?', (json.dumps(binding), step))
        elif mutation == 'result':
            result = json.loads(row['result_json'])
            if kind == 'WEB':
                result['summary'] = 'Changed otherwise schema-valid summary'
            else:
                result['items'][0]['summary'] = 'Changed otherwise schema-valid summary'
            store.conn.execute('UPDATE model_invocation_receipts SET result_json=? WHERE step=?', (json.dumps(result), step))
        elif mutation == 'request-hash':
            store.conn.execute("UPDATE model_invocation_receipts SET request_hash=? WHERE step=?", ('0' * 64, step))
        else:
            store.conn.execute('UPDATE model_invocation_receipts SET status=? WHERE step=?', (mutation, step))
        store.conn.commit()
        assert_rejected_without_effect(store, opportunity)
    finally:
        store.close()


@pytest.mark.parametrize('mutation', ['generation', 'items'])
def test_rss_changed_source_proof_never_creates_partial_reading(tmp_path, mutation):
    store, _, _, _, opportunity = interrupted_appraisal(tmp_path, 'RSS')
    try:
        row = store.conn.execute('SELECT result_json FROM capability_executions').fetchone()
        result = json.loads(row[0])
        if mutation == 'generation':
            result['data']['items'][0]['source_generation'] += 1
        else:
            result['data']['items'].pop()
        store.conn.execute('UPDATE capability_executions SET result_json=?', (json.dumps(result),))
        store.conn.commit()
        assert_rejected_without_effect(store, opportunity)
        assert {row[0] for row in store.conn.execute('SELECT status FROM world_rss_readings')} == {'STARTED'}
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['WEB', 'RSS'])
def test_explicit_promotion_and_local_effects_rollback_as_one_transaction(tmp_path, kind):
    store, _, model, _, opportunity = interrupted_appraisal(tmp_path, kind)
    try:
        store.conn.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'local event failed'); END")
        store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match='local event failed'):
            WorldBrowseRecovery(store).apply(opportunity)
        assert not store.list_events('c00')
        assert store.conn.execute('SELECT phase FROM world_browse_decisions').fetchone()[0] == 'APPRAISING'
        assert store.conn.execute('SELECT appraisal_json FROM world_browse_decisions').fetchone()[0] is None
        if kind == 'RSS':
            assert {row[0] for row in store.conn.execute('SELECT status FROM world_rss_readings')} == {'STARTED'}
        store.conn.execute('DROP TRIGGER reject_event')
        store.conn.commit()
        result = WorldBrowseRecovery(store).apply(opportunity)
        assert result['kept'] and len(model.calls) == 2
    finally:
        store.close()


def test_two_connections_apply_one_proven_appraisal_once(tmp_path):
    from character_memory.storage.sqlite import SQLiteStore
    store, _, model, _, opportunity = interrupted_appraisal(tmp_path)
    second = SQLiteStore(store.path)
    gate = threading.Barrier(2)
    def apply(connection):
        gate.wait()
        return WorldBrowseRecovery(connection).apply(opportunity)
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(apply, [store, second]))
        assert results[0] == results[1]
        assert len(store.list_events('c00')) == 1 and len(model.calls) == 2
    finally:
        second.close()
        store.close()


def test_scheduler_leaves_appraising_for_explicit_recovery(tmp_path):
    store, access, model, service, opportunity = interrupted_appraisal(tmp_path)
    try:
        access.settings.world_pulse_enabled = False
        access.settings.world_browse_enabled = False
        scheduler = WorldActivityScheduler(access, service.repository)
        result = scheduler.run_once(now=NOW + timedelta(days=1))
        assert not any(item['kind'] == 'BROWSE_RECOVERY' for item in result)
        assert store.conn.execute('SELECT phase FROM world_browse_decisions').fetchone()[0] == 'APPRAISING'
        assert not store.list_events('c00') and len(model.calls) == 2
    finally:
        store.close()


def test_unknown_backlog_does_not_starve_original_automatic_recovery(tmp_path):
    store, access, model, service, opportunity = interrupted_appraisal(tmp_path)
    try:
        saved = store.conn.execute(
            "SELECT result_json FROM model_invocation_receipts WHERE opportunity_id=? AND step='WEB_APPRAISAL'",
            (opportunity,),
        ).fetchone()[0]
        store.conn.execute("UPDATE world_browse_decisions SET phase='APPRAISED',appraisal_json=? WHERE opportunity_id=?",(saved,opportunity))
        store.conn.executemany(
            "INSERT INTO world_browse_decisions(opportunity_id,character_id,started_at,phase) VALUES(?,'c00','2000-01-01T00:00:00+00:00','APPRAISING')",
            [(f'old-unknown-{i}',) for i in range(201)],
        )
        store.conn.commit()
        access.settings.world_pulse_enabled = access.settings.world_browse_enabled = False
        result = WorldActivityScheduler(access, service.repository).run_once(now=NOW + timedelta(days=1))
        assert len([item for item in result if item['kind']=='BROWSE_RECOVERY' and item['status']=='OK'])==1
        assert len(store.list_events('c00'))==1 and len(model.calls)==2
    finally:
        store.close()


def test_verified_receipt_read_is_provider_free_and_read_only(tmp_path):
    from character_memory.runtime.model_execution import verified_structured_receipt
    from character_memory.world_activity import PersonalBrowseAppraisal
    store, _, model, _, opportunity = interrupted_appraisal(tmp_path)
    def read_only(action, *args):
        return sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE} else sqlite3.SQLITE_OK
    try:
        prompt = model.calls[-1][2]
        changes = store.conn.total_changes
        store.conn.set_authorizer(read_only)
        result = verified_structured_receipt(store, character_id='c00', opportunity_id=opportunity,
            step='WEB_APPRAISAL', prompt=prompt, schema=PersonalBrowseAppraisal,
            session_id='personal-browse-appraise:c00:' + opportunity)
        assert result.keep and store.conn.total_changes == changes
        assert len(model.calls) == 2
    finally:
        store.conn.set_authorizer(None)
        store.close()
