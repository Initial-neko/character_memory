from datetime import timedelta

import pytest

from character_memory.rss_sources import ParsedFeedItem, RssRepository
from character_memory.rss_world import RssPersonalReading
from character_memory.storage.sqlite import SQLiteStore
from test_world_activity import NOW


def fixture(tmp_path, count=3):
    store = SQLiteStore(tmp_path / 'rss-world.db')
    rss = RssRepository(store)
    source = rss.create_source('https://example.com/feed.xml', name='Public feed')
    rss.upsert_items(source['id'], [ParsedFeedItem(str(i), f'Topic {i}', 'summary', f'content {i}',
        f'https://example.com/{i}', '', NOW) for i in range(count)], fetched_at=NOW)
    return store, rss, source, RssPersonalReading(store)


def test_candidates_are_bounded_enabled_and_do_not_claim_shared_collection_is_read(tmp_path):
    store, rss, source, reading = fixture(tmp_path, 12)
    assert len(reading.candidates('a', limit=8)) == 8
    assert len(reading.candidates('a', limit=4)) == 4
    assert reading.candidates('a') == reading.candidates('b')
    assert store.list_events('a') == [] and store.list_memories('a') == []
    rss.set_enabled(source['id'], False)
    assert reading.candidates('a') == []
    rss.set_enabled(source['id'], True)
    rss.cancel_source(source['id'])
    assert reading.candidates('a') == []
    store.close()


def test_local_read_snapshot_is_exact_bounded_and_personal(tmp_path):
    store, rss, source, reading = fixture(tmp_path)
    selected = reading.candidates('a')[:2]
    result = reading.read('a', 'request:a', selected, max_items=2, max_chars=600)
    assert len(result['items']) == 2
    assert sum(len(item['content']) for item in result['items']) <= 600
    assert all(item['content_hash'] and item['source_generation'] == 0 for item in result['items'])
    assert all(item['content_scope'] == 'RSS_FEED_TEXT' for item in result['items'])
    assert len(reading.candidates('a')) == 1
    assert len(reading.candidates('b')) == 3
    assert reading.read('a', 'different-request', selected, max_items=2, max_chars=600)['items'] == []
    assert not store.list_events('a')
    store.close()


def test_cancel_restore_generation_is_checked_after_selection(tmp_path):
    store, rss, source, reading = fixture(tmp_path)
    selected = reading.candidates('a')[:1]
    rss.cancel_source(source['id'])
    rss.restore_source(source['id'])
    assert reading.read('a', 'old-request', selected, max_items=2, max_chars=600)['items'] == []
    assert not store.list_events('a')
    store.close()


def test_signal_ignores_refresh_clock_but_not_content_change(tmp_path):
    store, rss, source, reading = fixture(tmp_path)
    before = reading.signal('a')
    rss.mark_fetch(source['id'], now=NOW + timedelta(hours=1))
    assert reading.signal('a') == before
    rss.upsert_items(source['id'], [ParsedFeedItem('2', 'Updated title', 'summary', 'new content',
        'https://example.com/2', '', NOW)], fetched_at=NOW + timedelta(hours=1))
    assert reading.signal('a') != before
    store.close()


def test_known_failure_has_one_future_retry_but_unknown_or_ignored_never_retries(tmp_path):
    store, rss, source, reading = fixture(tmp_path)
    selected = reading.candidates('a')[:1]
    assert reading.read('a', 'request:1', selected, max_items=2, max_chars=600)['items']
    reading.finish('a', 'request:1', selected[0]['item_id'], status='FAILED')
    assert any(item['item_id'] == selected[0]['item_id'] for item in reading.candidates('a'))
    assert reading.read('a', 'request:2', selected, max_items=2, max_chars=600)['items']
    reading.finish('a', 'request:2', selected[0]['item_id'], status='FAILED')
    assert all(item['item_id'] != selected[0]['item_id'] for item in reading.candidates('a'))
    selected = reading.candidates('a')[:1]
    reading.read('a', 'request:ignored', selected, max_items=2, max_chars=600)
    reading.finish('a', 'request:ignored', selected[0]['item_id'], status='IGNORED')
    store.close()
    store = SQLiteStore(tmp_path / 'rss-world.db')
    assert len(RssPersonalReading(store).candidates('a')) == 1
    store.close()


def test_rss_executor_reads_local_text_without_web_or_extra_model(tmp_path):
    from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityRequest
    store, rss, source, reading = fixture(tmp_path)
    selected = reading.candidates('a')[:2]
    request = CapabilityRequest('rss-request:1', 'a', 'rss-op:1', None, 'WORLD', 'READ_RSS',
        {'items': [{'item_id': item['item_id'], 'source_generation': item['source_generation']} for item in selected]},
        {'max_items': 2, 'max_chars': 12000, 'max_calls': 1})
    class NoWeb:
        def observe(self, *args, **kwargs):
            raise AssertionError('local RSS must not fetch a website')
    executor = CapabilityExecutor(store, NoWeb(), rss_reader=reading)
    result = executor.execute(request, now=NOW)
    assert result.status == 'SUCCESS' and len(result.data['items']) == 2
    assert executor.execute(request, now=NOW) == result
    assert store.list_events('a') == []
    store.close()


def consumer_fixture(tmp_path, *, choice='READ_RSS', keep=True):
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository, PersonalWorldReadPlan, RssBatchAppraisal, RssItemAppraisal
    from test_world_activity import make_access
    store, access, model = make_access(tmp_path, count=1)
    access.settings.world_rss_reading_enabled = True
    rss = RssRepository(store)
    source = rss.create_source('https://example.com/feed.xml', name='Public feed')
    rss.upsert_items(source['id'], [ParsedFeedItem(str(i), f'Topic {i}', 'summary', f'content {i}',
        f'https://example.com/{i}', '', NOW) for i in range(3)], fetched_at=NOW)
    reading = RssPersonalReading(store)
    selected_ids = [item['item_id'] for item in reading.candidates('c00')[:2]]
    original = model.structured_for_session
    def decide(prompt, schema, session_id):
        if schema is PersonalWorldReadPlan:
            model.calls.append((schema.__name__, session_id, prompt))
            return PersonalWorldReadPlan(choice=choice, query='memory architecture', item_ids=selected_ids)
        if schema is RssBatchAppraisal:
            model.calls.append((schema.__name__, session_id, prompt))
            return RssBatchAppraisal(items=[RssItemAppraisal(item_id=item, keep=keep, summary='read feed text',
                personal_note='interesting content') for item in selected_ids])
        return original(prompt, schema, session_id)
    model.structured_for_session = decide
    return store, access, model, rss, source, reading, WorldActivityService(access, WorldPulseRepository(store))


def test_rss_consumer_batches_two_items_into_one_appraisal_and_real_observations(tmp_path):
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    def no_web(*args, **kwargs):
        raise AssertionError('RSS must not visit website')
    access.world_observer.observe = no_web
    result = service.browse_character('c00', now=NOW, opportunity_id='rss-op:1')
    assert result['reading_choice'] == 'READ_RSS' and result['execution_status'] == 'SUCCESS'
    assert len(model.calls) == 2
    assert len(store.list_events('c00')) == 2 and not store.list_memories('c00')
    for event in store.list_events('c00'):
        assert event.metadata['channel'] == 'PERSONAL_RSS'
        assert event.metadata['rss_reading']['content_scope'] == 'RSS_FEED_TEXT'
        assert event.metadata['sources'] and event.metadata['rss_reading']['content_hash']
    assert service.browse_character('c00', now=NOW, opportunity_id='rss-op:1') == result
    assert len(model.calls) == 2
    store.close()


def test_rss_consumer_ignored_items_are_personal_receipts_without_experience(tmp_path):
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path, keep=False)
    result = service.browse_character('c00', now=NOW, opportunity_id='rss-op:ignored')
    assert not result['kept'] and not store.list_events('c00')
    assert len(model.calls) == 2 and len(reading.candidates('c00')) == 1
    assert all(row[0]=='IGNORED' for row in store.conn.execute('SELECT status FROM world_rss_readings'))
    store.close()


@pytest.mark.parametrize('choice,expected_calls', [('NO_ACTION', 1), ('WEB_SEARCH', 2)])
def test_rss_presence_does_not_force_reading_or_publication(tmp_path, choice, expected_calls):
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path, choice=choice)
    result = service.browse_character('c00', now=NOW, opportunity_id='rss-op:optional')
    assert len(model.calls) == expected_calls
    assert len(reading.candidates('c00')) == 3
    assert store.conn.execute('SELECT count(*) FROM world_rss_readings').fetchone()[0] == 0
    store.close()


def test_rss_idle_signal_changes_only_on_eligible_content(tmp_path):
    from character_memory.world_activity import WorldActivityScheduler, WorldPulseRepository
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path, choice='NO_ACTION')
    access.settings.world_pulse_enabled = False
    repo = WorldPulseRepository(store)
    repo.ensure_state('BROWSE', 'c00', NOW, delay_minutes=0, interval_minutes=90)
    scheduler = WorldActivityScheduler(access, repo)
    scheduler.run_once(now=NOW)
    assert len(model.calls) == 1
    rss.mark_fetch(source['id'], now=NOW + timedelta(minutes=1))
    assert scheduler._browse_plan_due('c00', NOW + timedelta(minutes=5), base_minutes=90) == (False, 'IDLE_NO_NEW_SIGNAL')
    rss.upsert_items(source['id'], [ParsedFeedItem('3', 'Fresh article', 'summary', 'fresh content',
        'https://example.com/3', '', NOW)], fetched_at=NOW + timedelta(minutes=2))
    assert scheduler._browse_plan_due('c00', NOW + timedelta(minutes=5), base_minutes=90) == (True, 'NEW_LOCAL_RSS_CONTENT')
    assert len(model.calls) == 1
    store.close()


def test_rss_appraisal_duplicate_id_is_rejected_without_fabricating_experience(tmp_path):
    from character_memory.world_activity import RssBatchAppraisal, RssItemAppraisal
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    original = model.structured_for_session
    first = reading.candidates('c00')[0]['item_id']
    def invalid(prompt, schema, session):
        result = original(prompt, schema, session)
        if schema is RssBatchAppraisal:
            return RssBatchAppraisal(items=[RssItemAppraisal(item_id=first, keep=True, summary='read'),
                                           RssItemAppraisal(item_id=first, keep=True, summary='read')])
        return result
    model.structured_for_session = invalid
    with pytest.raises(ValueError, match='INVALID_RSS_APPRAISAL'):
        service.browse_character('c00', now=NOW, opportunity_id='rss-op:invalid')
    assert not store.list_events('c00') and len(model.calls) == 2
    assert all(row[0]=='FAILED' for row in store.conn.execute('SELECT status FROM world_rss_readings'))
    store.close()


def test_rss_local_commit_failure_resumes_without_repeating_batch_appraisal(tmp_path):
    import sqlite3
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    store.conn.execute("CREATE TRIGGER rss_event_fault BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'local RSS fault'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='local RSS fault'):
        service.browse_character('c00', now=NOW, opportunity_id='rss-op:recover')
    assert len(model.calls)==2 and not store.list_events('c00')
    assert all(row[0]=='STARTED' for row in store.conn.execute('SELECT status FROM world_rss_readings'))
    store.conn.execute('DROP TRIGGER rss_event_fault')
    store.conn.commit()
    result = service.browse_character('c00', now=NOW + timedelta(hours=1), opportunity_id='rss-op:recover')
    assert result['kept'] and len(model.calls)==2 and len(store.list_events('c00'))==2
    assert all(event.event_time==NOW for event in store.list_events('c00'))
    store.close()


def test_rss_unknown_appraisal_is_not_repeated_after_restart(tmp_path):
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository, RssBatchAppraisal
    from test_world_activity import make_access
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    original = model.structured_for_session
    def interrupt(*args):
        if args[1] is RssBatchAppraisal:
            raise KeyboardInterrupt('appraisal outcome unknown')
        return original(*args)
    model.structured_for_session=interrupt
    with pytest.raises(KeyboardInterrupt):
        service.browse_character('c00', now=NOW, opportunity_id='rss-op:unknown')
    store.close()
    store, access, model = make_access(tmp_path, count=1)
    access.settings.world_rss_reading_enabled=True
    reading=RssPersonalReading(store)
    result = WorldActivityService(access,WorldPulseRepository(store)).browse_character('c00',now=NOW,opportunity_id='rss-op:unknown')
    assert result['opportunity_status']=='UNKNOWN' and model.calls==[]
    assert len(reading.candidates('c00'))==1 and not store.list_events('c00')
    store.close()


def test_rss_candidate_selection_never_admits_unknown_item_ids(tmp_path):
    from character_memory.world_activity import PersonalWorldReadPlan
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    original=model.structured_for_session
    def wrong_id(*args):
        value=original(*args)
        return PersonalWorldReadPlan(choice='READ_RSS',item_ids=[99999]) if args[1] is PersonalWorldReadPlan else value
    model.structured_for_session=wrong_id
    with pytest.raises(ValueError,match='INVALID_RSS_SELECTION'):
        service.browse_character('c00',now=NOW,opportunity_id='rss-op:foreign')
    assert len(model.calls)==1 and not store.list_events('c00')
    assert store.conn.execute('SELECT count(*) FROM world_rss_readings').fetchone()[0]==0
    store.close()


@pytest.mark.parametrize('mode', ['disabled', 'empty', 'cancelled'])
def test_disabled_or_empty_rss_keeps_original_web_schema_and_prompt(tmp_path, mode):
    from test_world_activity import make_access
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository
    store, access, model = make_access(tmp_path, count=1)
    service=WorldActivityService(access,WorldPulseRepository(store))
    service.browse_character('c00',now=NOW,opportunity_id='before')
    original=[(name,prompt) for name,session,prompt in model.calls]
    model.calls.clear()
    access.settings.world_rss_reading_enabled=mode != 'disabled'
    if mode=='cancelled':
        rss=RssRepository(store)
        source=rss.create_source('https://example.com/feed.xml',name='Cancelled')
        rss.upsert_items(source['id'],[ParsedFeedItem('1','Title','summary','unread text','https://example.com/1','',NOW)],fetched_at=NOW)
        rss.cancel_source(source['id'])
    service.browse_character('c00',now=NOW,opportunity_id='after')
    assert [(name,prompt) for name,session,prompt in model.calls]==original
    store.close()


def test_two_connections_cannot_acquire_same_personal_rss_item(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store,rss,source,reading=fixture(tmp_path)
    selected=reading.candidates('a')[:1]
    other=SQLiteStore(tmp_path/'rss-world.db')
    second=RssPersonalReading(other)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(reading.read,'a','request:1',selected,max_items=2,max_chars=600)
            two=pool.submit(second.read,'a','request:2',selected,max_items=2,max_chars=600)
            assert len(one.result()['items'])+len(two.result()['items'])==1
        assert store.conn.execute('SELECT attempts FROM world_rss_readings').fetchone()[0]==1
    finally:
        other.close()
        store.close()
