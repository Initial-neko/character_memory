from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from character_memory.config import Settings
from character_memory.world_activity import WorldActivityService, WorldPulseRepository
from test_world_activity import make_access, NOW


def setup(tmp_path, decisions):
    store, access, model = make_access(tmp_path, count=2)
    object.__setattr__(access.settings, 'world_continuation_enabled', True)
    prompts = []
    def structured(prompt, schema, session):
        prompts.append(prompt)
        return schema.model_validate(decisions.pop(0))
    model.structured_for_session = structured
    return store, access, model, prompts, WorldActivityService(access, WorldPulseRepository(store))


def test_continuation_setting_defaults_off():
    assert getattr(Settings(), 'world_continuation_enabled', None) is False


def test_stop_is_silent_durable_and_not_replayed(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [{'action':'STOP'}])
    result = service.browse_character('c00', now=NOW, opportunity_id='stop')
    assert result['continuation'] and result['steps'] == [] and not result['kept']
    assert len(prompts) == 1 and store.list_events('c00') == []
    assert service.browse_character('c00', now=NOW, opportunity_id='stop') == result
    assert len(prompts) == 1
    store.close()


def test_real_receipt_reaches_next_decision_and_person_note_stays_local(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL', 'capability':'WEB_SEARCH', 'arguments':{'query':'agent memory'}},
        {'action':'STOP','keep':True,'summary':'读到真实来源','personal_note':'我对这段内容有兴趣'},
    ])
    result = service.browse_character('c00', now=NOW, opportunity_id='read')
    assert len(prompts) == 2 and 'UNTRUSTED' in prompts[1]
    assert 'https://example.com/agent-memory' in prompts[1]
    assert result['kept'] and len(result['steps']) == 1
    events = store.list_events('c00')
    assert len(events) == 1 and events[0].content == '我对这段内容有兴趣'
    assert events[0].metadata['source_receipts'] == [result['steps'][0]['request_id']]
    assert store.list_events('c01') == []
    store.close()


def test_cannot_keep_unread_plan_and_unavailable_browser_not_exposed(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [{'action':'STOP','keep':True,'summary':'编造已读'}])
    result = service.browse_character('c00', now=NOW, opportunity_id='unread')
    assert not result['kept'] and not store.list_events('c00')
    assert 'BROWSER_OPEN' not in prompts[0]
    store.close()


def test_three_attempts_end_without_fourth_model_call(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':f'query {i}'}} for i in range(3)
    ])
    result = service.browse_character('c00', now=NOW, opportunity_id='limit')
    assert len(prompts) == len(result['steps']) == 3
    assert result['stop_reason'] == 'STEP_BUDGET_EXHAUSTED'
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 3
    store.close()


def test_interrupted_model_or_tool_phase_is_never_replayed(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [])
    service.repository.claim_browse_decision('interrupted', 'c00', NOW)
    service.repository.update_browse_decision('interrupted', expected_phase='PLANNING', phase='CONT_MODEL:1', plan={'mode':'CONTINUATION'})
    result = service.browse_character('c00', now=NOW, opportunity_id='interrupted')
    assert result['execution_status'] == 'UNKNOWN' and not prompts
    assert service.browse_character('c00', now=NOW, opportunity_id='interrupted')['execution_status'] == 'UNKNOWN'
    store.close()



def test_model_arguments_cannot_add_identity_or_grants(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'x','character_id':'c01'}},
        {'action':'STOP'},
    ])
    result = service.browse_character('c00', now=NOW, opportunity_id='identity')
    assert result['steps'][0]['status'] == 'DENIED'
    assert result['steps'][0]['reason'] == 'INVALID_ARGUMENTS'
    assert not store.list_events('c00') and not store.list_events('c01')
    store.close()


def test_concurrent_repeat_cannot_start_second_model(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [{'action':'STOP'}])
    original = model.structured_for_session
    seen = []
    def while_planning(prompt, schema, session):
        seen.append(service.browse_character('c00', now=NOW, opportunity_id='concurrent'))
        return original(prompt, schema, session)
    model.structured_for_session = while_planning
    result = service.browse_character('c00', now=NOW, opportunity_id='concurrent')
    assert seen[0]['execution_status'] == 'UNKNOWN'
    assert len(prompts) == 1 and result['opportunity_status'] == 'APPLIED'
    store.close()


def test_model_timeout_is_unknown_and_never_replayed(tmp_path):
    import time
    store, access, model, prompts, service = setup(tmp_path, [])
    object.__setattr__(access.settings, 'world_continuation_max_seconds', .03)
    calls, finished = [], threading.Event()
    def slow(*args):
        calls.append(True)
        time.sleep(.15)
        finished.set()
        return {'action':'STOP'}
    model.structured_for_session = slow
    result = service.browse_character('c00', now=NOW, opportunity_id='timeout')
    assert result['execution_status'] == 'UNKNOWN'
    assert service.browse_character('c00', now=NOW, opportunity_id='timeout') == result
    assert calls == [True]
    assert finished.wait(1)
    store.close()


def test_failure_is_data_for_reconsideration_not_success(tmp_path):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'x'}},
        {'action':'STOP','keep':True,'summary':'must not retain'},
    ])
    def failed(*args, **kwargs):
        raise TimeoutError('network unavailable')
    access.world_observer.observe = failed
    result = service.browse_character('c00', now=NOW, opportunity_id='failure')
    assert result['steps'][0]['status'] == 'TIMEOUT'
    assert 'network unavailable' in prompts[1]
    assert not result['kept'] and not store.list_events('c00')
    store.close()


def test_db_failure_after_external_receipt_cannot_replay(tmp_path, monkeypatch):
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'x'}},
    ])
    original = service.repository.update_browse_decision
    calls = []
    observer = access.world_observer.observe
    def observed(*args, **kwargs):
        calls.append(True)
        return observer(*args, **kwargs)
    access.world_observer.observe = observed
    def fail_received(opportunity, **kwargs):
        if kwargs['phase'].startswith('CONT_RECEIVED') or kwargs['phase'] == 'FAILED':
            raise RuntimeError('database write failed')
        return original(opportunity, **kwargs)
    monkeypatch.setattr(service.repository, 'update_browse_decision', fail_received)
    with pytest.raises(RuntimeError, match='database write failed'):
        service.browse_character('c00', now=NOW, opportunity_id='db-failure')
    monkeypatch.setattr(service.repository, 'update_browse_decision', original)
    result = service.browse_character('c00', now=NOW, opportunity_id='db-failure')
    assert result['execution_status'] == 'UNKNOWN'
    assert calls == [True] and len(prompts) == 1
    store.close()


def test_issue_operator_configuration_defaults_to_no_write(tmp_path):
    from character_memory.config import load_settings
    defaults = Settings()
    assert getattr(defaults, 'github_issue_write_repositories', None) == []
    assert getattr(defaults, 'github_issue_write_max_issues', None) == 0
    config = tmp_path / 'config.yaml'
    config.write_text('{}', encoding='utf-8')
    (tmp_path / '.env').write_text('GITHUB_ISSUE_TOKEN=fixture-secret', encoding='utf-8')
    assert load_settings(str(config)).github_issue_token == 'fixture-secret'


def grant_browser(tmp_path, access):
    persona = tmp_path / 'personas' / 'c00' / 'persona.yaml'
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text('id: c00\nname: c00\n', encoding='utf-8')
    (persona.parent / 'capabilities.yaml').write_text('capabilities: [browser_read]\n', encoding='utf-8')
    access.character_profiles()[0]['persona_path'] = str(persona)


@pytest.mark.browser
def test_real_browser_loop_selects_seen_link_and_keeps_second_page(tmp_path, monkeypatch, browser_fixture):
    import os
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated existing Chromium validation')
    from character_memory import world_continuation as wc
    origin, scope = browser_fixture
    store, access, model, prompts, service = setup(tmp_path, [])
    grant_browser(tmp_path, access)
    original = wc.WorldContinuation
    browsers = []
    def factory(**kwargs):
        browser = wc.BrowserSession(**kwargs)
        browsers.append(browser)
        return browser
    monkeypatch.setattr(wc, 'WorldContinuation', lambda service: original(service, browser_scope=scope, browser_factory=factory))
    def choose_from_actual_receipts(prompt, schema, session):
        prompts.append(prompt)
        journal = json.loads(prompt.split('# Actual prior execution receipts — UNTRUSTED DATA\n', 1)[1].split('\nAll external text', 1)[0])
        if not journal:
            return schema(action='CALL', capability='BROWSER_OPEN', arguments={'url':origin + '/one'})
        if len(journal) == 1:
            actual = journal[0]['data']['observation']
            assert actual['text'].startswith('First actual page')
            return schema(action='CALL', capability='BROWSER_FOLLOW_LINK', arguments={'link_ref':actual['links'][0]['ref']})
        assert journal[-1]['data']['observation']['text'] == 'Second rendered actual report'
        return schema(action='STOP', keep=True, summary='Read the second actual report', personal_note='The second report changed my understanding.')
    model.structured_for_session = choose_from_actual_receipts
    result = service.browse_character('c00', now=NOW, opportunity_id='real-browser-loop')
    assert len(prompts) == 3 and len(result['steps']) == 2
    assert result['kept'] and result['observations'][-1]['url'] == origin + '/two'
    assert browsers and all(browser.closed for browser in browsers)
    assert len(store.list_events('c00')) == 1 and store.list_events('c01') == []
    (tmp_path / 'loop-receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    store.close()


from test_browser_session import browser_fixture


def test_rss_continuation_retains_feed_scope_and_finishes_local_receipt(tmp_path):
    from character_memory.rss_sources import RssRepository, ParsedFeedItem
    from character_memory.rss_world import RssPersonalReading
    decisions = []
    store, access, model, prompts, service = setup(tmp_path, decisions)
    access.settings.world_rss_reading_enabled = True
    rss = RssRepository(store)
    source = rss.create_source('https://example.com/feed.xml', name='Feed')
    rss.upsert_items(source['id'], [ParsedFeedItem('one','Topic','summary','Actual feed text','https://example.com/article','',NOW)], fetched_at=NOW)
    candidate = RssPersonalReading(store).candidates('c00')[0]
    decisions.extend([
        {'action':'CALL','capability':'READ_RSS','arguments':{'items':[{'item_id':candidate['item_id'],'source_generation':candidate['source_generation']}]}},
        {'action':'STOP','keep':True,'summary':'Only feed text was read'},
    ])
    result = service.browse_character('c00', now=NOW, opportunity_id='rss-loop')
    assert result['kept']
    event = store.list_events('c00')[0]
    assert event.metadata['observation_lifecycle']['reading_scope'] == 'RSS_FEED_TEXT'
    row = store.conn.execute('SELECT status,source_event_id FROM world_rss_readings WHERE character_id=?', ('c00',)).fetchone()
    assert row['status'] == 'APPLIED' and row['source_event_id'] == event.id
    assert not store.list_events('c01')
    store.close()


def test_expired_deadline_does_not_start_provider():
    import time
    from character_memory.world_continuation import _bounded_call
    calls = []
    with pytest.raises(TimeoutError):
        _bounded_call(lambda:calls.append(True), time.monotonic() - 1, threading.BoundedSemaphore(1))
    assert calls == []


def test_context_recall_timeout_is_bounded(tmp_path):
    import time
    store, access, model, prompts, service = setup(tmp_path, [])
    object.__setattr__(access.settings, 'world_continuation_max_seconds', .03)
    builder = access.require_bundle().runtimes['c00'].context_builder
    original = builder.build
    done = threading.Event()
    def slow(*args, **kwargs):
        time.sleep(.15)
        value = original(*args, **kwargs)
        done.set()
        return value
    builder.build = slow
    started = time.monotonic()
    result = service.browse_character('c00', now=NOW, opportunity_id='context-timeout')
    assert result['execution_status'] == 'UNKNOWN'
    assert time.monotonic() - started < .1 and not prompts
    assert done.wait(1)
    store.close()


def test_thread_scheduled_after_deadline_never_starts_external_work(monkeypatch):
    import time
    from character_memory.world_continuation import _bounded_call
    original = threading.Thread.start
    def delayed(thread):
        time.sleep(.04)
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', delayed)
    calls = []
    with pytest.raises(TimeoutError):
        _bounded_call(lambda:calls.append(True), time.monotonic() + .02, threading.BoundedSemaphore(1))
    assert calls == []


def grant_permissions(tmp_path, access, permissions):
    grant_browser(tmp_path, access)
    persona = Path(access.character_profiles()[0]['persona_path'])
    (persona.parent / 'capabilities.yaml').write_text('capabilities: '+json.dumps(permissions)+'\n', encoding='utf-8')


def test_production_tools_discovery_is_read_only_and_draft_is_local(tmp_path):
    from character_memory.runtime.capability_execution import CapabilityRequest
    from character_memory.application.continuation_tools import build_world_continuation_tools
    store, access, model, prompts, service = setup(tmp_path, [])
    grant_permissions(tmp_path, access, ['github_issue_write'])
    before = store.conn.total_changes
    tools, adapters = service.continuation_tools('c00')
    assert [t.id for t in tools] == ['GITHUB_ISSUE_DRAFT']
    assert store.conn.total_changes == before
    request = CapabilityRequest('draft','c00','op',None,'WORLD','GITHUB_ISSUE_DRAFT',
        {'repository':'owner/repo','title':'Draft title','body':'Draft body'},{'max_calls':3})
    result = adapters['GITHUB_ISSUE_DRAFT'].execute(request)
    assert result.data['status'] == 'DRAFT' and result.data['external_write'] is False
    assert store.conn.execute('SELECT count(*) FROM collaboration_issue_writes').fetchone()[0] == 0
    grant_permissions(tmp_path, access, [])
    assert adapters['GITHUB_ISSUE_DRAFT'].validate(request) == 'ROLE_CAPABILITY_REVOKED'
    assert service.continuation_tools('c00') == ((), {})
    store.close()


def test_production_contact_uses_shared_channel_and_actual_reply(tmp_path):
    from character_memory.group_store import GroupRepository
    from character_memory.runtime.capability_execution import CapabilityRequest
    store, access, model, prompts, service = setup(tmp_path, [])
    grant_permissions(tmp_path, access, ['role_contact'])
    groups = GroupRepository(store)
    group = groups.create_group('Shared', ['c00','c01'], NOW)
    builder = access.require_bundle().runtimes['c01'].context_builder
    original = builder.build
    builder.build = lambda cid, query, **kw: original(cid, query, at=kw['at'], recent_limit=kw['recent_limit'])
    def reply(prompt, schema, session):
        prompts.append(prompt)
        return schema(message='A real independent shared reply')
    model.structured_for_session = reply
    before = store.conn.total_changes
    tools, adapters = service.continuation_tools('c00')
    assert store.conn.total_changes == before and tools[0].id == 'ROLE_CONTACT'
    ref = tools[0].arguments_schema['properties']['channel_ref']['enum'][0]
    request = CapabilityRequest('contact','c00','contact-op',9999,'WORLD','ROLE_CONTACT',
        {'recipient_id':'c01','channel_ref':ref,'topic':'Consider this public subject'},{'max_calls':3})
    result = adapters['ROLE_CONTACT'].execute(request)
    assert result.status == 'SUCCESS' and result.data['contact']['status'] == 'COMPLETED'
    assert result.data['contact']['source_event_id'] is None
    assert result.data['contact']['shared_reply']['content'] == 'A real independent shared reply'
    assert [e.actor_id for e in groups.list_events(group.id)] == ['c00','c01']
    assert len(prompts) == 1
    store.close()


def test_issue_create_uses_current_operator_scope_and_never_replays(tmp_path, monkeypatch):
    from character_memory.application import collaboration_runtime as cr
    from character_memory.runtime.capability_execution import CapabilityRequest
    store, access, model, prompts, service = setup(tmp_path, [])
    grant_permissions(tmp_path, access, ['github_issue_write'])
    access.settings.github_issue_write_repositories = ['owner/repo']
    access.settings.github_issue_write_operator_id = 'fixture-operator'
    access.settings.github_issue_write_max_issues = 1
    access.settings.github_issue_token = 'fixture-token'
    calls = []
    def transport(token):
        def post(**kwargs):
            calls.append(kwargs)
            return {'html_url':'https://github.com/owner/repo/issues/1'}
        return post
    monkeypatch.setattr(cr, 'GitHubIssueHTTPTransport', transport)
    tools, adapters = service.continuation_tools('c00')
    assert [t.id for t in tools] == ['GITHUB_ISSUE_DRAFT','GITHUB_ISSUE_CREATE']
    request = CapabilityRequest('issue-create','c00','issue-op',None,'WORLD','GITHUB_ISSUE_CREATE',
        {'repository':'owner/repo','title':'Title','body':'Body'},{'max_calls':3})
    first = adapters['GITHUB_ISSUE_CREATE'].execute(request)
    assert first.data['status'] == 'CREATED'
    assert adapters['GITHUB_ISSUE_CREATE'].execute(request).data == first.data
    assert len(calls) == 1
    access.settings.github_issue_write_repositories = []
    assert adapters['GITHUB_ISSUE_CREATE'].validate(request) == 'ISSUE_OPERATOR_SCOPE_DENIED'
    assert [t.id for t in service.continuation_tools('c00')[0]] == ['GITHUB_ISSUE_DRAFT']
    store.close()


def test_appraised_local_recovery_preserves_original_time_without_paid_replay(tmp_path, monkeypatch):
    from datetime import timedelta
    store, access, model, prompts, service = setup(tmp_path, [
        {'action':'CALL','capability':'WEB_SEARCH','arguments':{'query':'x'}},
        {'action':'STOP','keep':True,'summary':'Actual observation'},
    ])
    original = store.append_observation_once
    def failed(event):
        raise RuntimeError('local apply failed')
    monkeypatch.setattr(store, 'append_observation_once', failed)
    with pytest.raises(RuntimeError, match='local apply failed'):
        service.browse_character('c00', now=NOW, opportunity_id='local-recovery')
    assert service.repository.get_browse_decision('local-recovery')['phase'] == 'CONT_APPRAISED'
    monkeypatch.setattr(store, 'append_observation_once', original)
    result = service.browse_character('c00', now=NOW + timedelta(days=1), opportunity_id='local-recovery')
    assert result['kept'] and len(prompts) == 2
    assert store.list_events('c00')[0].event_time == NOW
    assert store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0] == 1
    store.close()


def test_next_world_prompt_reads_real_collaboration_after_store_restart(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.group_store import GroupRepository
    from character_memory.application.role_collaboration import RoleCollaborationService, CollaborationContext
    from character_memory.runtime.person_context import PersonContextBuilder
    store, access, model = make_access(tmp_path, count=3)
    group=GroupRepository(store).create_group('Shared',['c00','c01'],NOW)
    contact=RoleCollaborationService(store,GroupRepository(store),None,profiles=access.character_profiles,
        reply_decider=lambda *args:'We actually discussed this exact boundary')
    receipt=contact.contact('c01','Discuss the observed boundary',context=CollaborationContext('c00','GROUP',group.id,'prior-contact',None,NOW))
    assert receipt['status']=='COMPLETED'
    store.close()
    reopened=SQLiteStore(tmp_path/'world.db')
    access.store=lambda:reopened
    access.read_store=reopened
    access.settings.world_continuation_enabled=True
    recall=SimpleNamespace(recall=lambda *args,**kwargs:[])
    for cid,runtime in access.require_bundle().runtimes.items():
        runtime.context_builder=PersonContextBuilder(reopened,recall,runtime.persona)
    prompts=[]
    def stop(prompt,schema,session):
        prompts.append(prompt)
        return schema(action='STOP')
    model.structured_for_session=stop
    service=WorldActivityService(access,WorldPulseRepository(reopened))
    for cid in ('c00','c01','c02'):
        service.browse_character(cid,now=NOW+timedelta(minutes=1),opportunity_id=f'next:{cid}')
    for prompt in prompts[:2]:
        assert 'We actually discussed this exact boundary' in prompt
        assert 'prior-contact' in prompt and 'shared_fact_id' in prompt and 'GROUP' in prompt
    assert 'We actually discussed this exact boundary' not in prompts[2]
    assert not reopened.list_memories('c00')
    reopened.close()
