from datetime import datetime, timezone
from types import SimpleNamespace
from character_memory.application.role_collaboration import CollaborationContext, RoleCollaborationService
from character_memory.storage.sqlite import SQLiteStore
from character_memory.group_store import GroupRepository


def test_group_real_identity_and_replay(tmp_path):
    store = SQLiteStore(tmp_path / 'c.db')
    groups = GroupRepository(store)
    now = datetime.now(timezone.utc)
    group = groups.create_group('research', ['alice', 'claude'], now)
    service = RoleCollaborationService(store, groups, None, profiles=lambda: [{'id':'alice'}, {'id':'claude'}], reply_decider=lambda *args: 'I disagree')
    context = CollaborationContext('alice', 'GROUP', group.id, 'op1', None, now)
    receipt = service.contact('claude', 'Please consider this', context=context)
    assert receipt['status'] == 'COMPLETED'
    assert service.contact('claude', 'Please consider this', context=context) == receipt
    assert [e.actor_id for e in groups.list_events(group.id)] == ['alice', 'claude']
    assert service.contact('user', 'hello', context=context)['status'] == 'DENIED'

from character_memory.application.role_collaboration import CollaborationReply, GitHubIssueAdapter, IssueWriteGrant
from character_memory.runtime.capability_execution import CapabilityResult
from character_memory.space_store import SpaceRepository


def make_service(tmp_path, decider, **kwargs):
    store = SQLiteStore(tmp_path / 'roles.db')
    groups, space = GroupRepository(store), SpaceRepository(store)
    now = datetime.now(timezone.utc)
    service = RoleCollaborationService(store, groups, space,
        profiles=lambda:[{'id':'alice'}, {'id':'claude'}], reply_decider=decider, **kwargs)
    return store, groups, space, service, now


def test_space_real_reply_chain_never_notifies_user(tmp_path):
    store, groups, space, service, now = make_service(tmp_path,lambda *args:'No, evidence is weak')
    post = space.create_post('alice','observation',now)
    c = CollaborationContext('alice','SPACE',str(post.id),'space1',None,now)
    r = service.contact('claude','Your opinion?',context=c)
    comments = space.list_comments(post.id)
    assert [x.character_id for x in comments] == ['alice','claude']
    assert comments[1].reply_to_comment_id == comments[0].id
    assert space.unread_notification_count() == 0
    assert r['reply_event_id'] == comments[1].id
    assert service.eligible_recipients('alice')[0]['channels'] == [{'channel':'SPACE','channel_id':str(post.id)}]


def test_research_requires_recipient_choice_and_trusted_grant(tmp_path):
    calls=[]
    def research(request):
        calls.append(request)
        return CapabilityResult(request.request_id,'COMPLETED',{'sources':['https://example.org']})
    choices=[]
    def decide(recipient,topic,result,context):
        choices.append(result)
        return CollaborationReply(research_query='public research') if result is None else CollaborationReply(message='sources disagree')
    store, groups, space, service, now = make_service(tmp_path,decide,research=research,research_policy=lambda r:{'max_pages':1})
    group=groups.create_group('g',['alice','claude'],now)
    c=CollaborationContext('alice','GROUP',group.id,'research1',None,now)
    assert service.contact('claude','question',context=c)['status']=='COMPLETED'
    assert len(calls)==1 and len(choices)==2
    assert calls[0].character_id=='claude' and calls[0].channel=='WORLD'
    assert calls[0].arguments=={'query':'public research'}
    service.reply_decider=lambda *args:None
    c=CollaborationContext('alice','GROUP',group.id,'research2',None,now)
    assert service.contact('claude','question',context=c)['status']=='SILENT'
    assert len(calls)==1


def test_unknown_no_replay_conflict_and_channel_denial(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:None)
    group=groups.create_group('g',['alice','claude'],now)
    c=CollaborationContext('alice','GROUP',group.id,'once',None,now)
    first=service.contact('claude','question',context=c)
    import json
    first['status']='RUNNING'
    with store.transaction():
        store.conn.execute('UPDATE role_collaboration_receipts SET receipt_json=?',(json.dumps(first),))
    assert service.contact('claude','question',context=c)['status']=='UNKNOWN'
    assert len(groups.list_events(group.id))==1
    assert service.contact('claude','changed',context=c)['reason']=='IDEMPOTENCY_CONFLICT'
    bad=CollaborationContext('alice','DIRECT','claude','bad',None,now)
    assert service.contact('claude','hi',context=bad)['status']=='DENIED'
    private=space.create_post('alice','private',now,visibility='PRIVATE')
    assert service.contact('claude','hi',context=CollaborationContext('alice','SPACE',str(private.id),'bad',None,now))['status']=='DENIED'


def test_issue_grant_scope_quota_and_timeout_no_replay(tmp_path):
    store=SQLiteStore(tmp_path/'issues.db')
    calls=[]
    def transport(**kw):
        calls.append(kw)
        raise TimeoutError()
    adapter=GitHubIssueAdapter(store,transport)
    args=dict(character_id='claude',request_id='1',repository='owner/repo',title='evidence',body='steps')
    assert adapter.create(**args)['status']=='DRAFT' and not calls
    grant=IssueWriteGrant('operator',('owner/repo',),1,'claude')
    r=adapter.create(**args,grant=grant)
    assert r['status']=='UNKNOWN' and 'url' not in r
    assert adapter.create(**args,grant=grant)==r and len(calls)==1
    assert adapter.create(**{**args,'request_id':'2'},grant=grant)['reason']=='QUOTA_EXHAUSTED'
    assert adapter.create(**{**args,'repository':'other/repo'},grant=grant)['reason']=='GRANT_DENIED'


def test_membership_source_and_archived_recipient_denied(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:'reply')
    group=groups.create_group('g',['alice'],now)
    c=CollaborationContext('alice','GROUP',group.id,'x',None,now)
    assert service.contact('claude','hello',context=c)['reason']=='CHANNEL_MEMBERSHIP_DENIED'
    group=groups.create_group('g2',['alice','claude'],now)
    c=CollaborationContext('alice','GROUP',group.id,'x',99999,now)
    assert service.contact('claude','hello',context=c)['reason']=='SOURCE_DENIED'
    service.profiles=lambda:[{'id':'alice'},{'id':'claude','archived_at':'2026-10-16'}]
    assert service.contact('claude','hello',context=c)['reason']=='INVALID_PARTICIPANTS'


def test_no_research_without_grant_and_failed_reply_not_retried(tmp_path):
    calls=[]
    store,groups,space,service,now=make_service(tmp_path,
        lambda *args:CollaborationReply(research_query='query'),research=lambda r:calls.append(r))
    group=groups.create_group('g',['alice','claude'],now)
    c=CollaborationContext('alice','GROUP',group.id,'x',None,now)
    assert service.contact('claude','question',context=c)['status']=='SILENT'
    assert not calls
    def fail(*args):
        calls.append('decision')
        raise RuntimeError('private failure detail')
    service.reply_decider=fail
    c=CollaborationContext('alice','GROUP',group.id,'y',None,now)
    r=service.contact('claude','question',context=c)
    assert r['status']=='FAILED' and r['reason']=='RuntimeError'
    assert service.contact('claude','question',context=c)==r
    assert calls==['decision']


def test_issue_success_and_bad_url_are_durable(tmp_path):
    store=SQLiteStore(tmp_path/'success.db')
    calls=[]
    def transport(**kw):
        calls.append(kw)
        return {'html_url':'https://github.com/owner/repo/issues/7'}
    adapter=GitHubIssueAdapter(store,transport)
    grant=IssueWriteGrant('operator',('owner/repo',),2,'claude')
    args=dict(character_id='claude',request_id='1',repository='owner/repo',title='evidence',body='steps',grant=grant)
    r=adapter.create(**args)
    assert r['status']=='CREATED' and r['url'].endswith('/issues/7')
    assert adapter.create(**args)==r and len(calls)==1
    assert calls[0]['timeout_seconds']==20 and calls[0]['idempotency_key']=='1'
    adapter.transport=lambda **kw:{'html_url':'https://example.org/invented'}
    assert adapter.create(**{**args,'request_id':'2'})['status']=='UNKNOWN'

import pytest


@pytest.mark.parametrize('channel', ['GROUP', 'SPACE'])
@pytest.mark.parametrize('archived_id', ['alice', 'claude'])
def test_archive_during_decision_blocks_reply(tmp_path, channel, archived_id):
    profiles = [{'id':'alice'}, {'id':'claude'}]
    def decide(*args):
        next(p for p in profiles if p['id']==archived_id)['archived_at']=None
        return 'reply must not be sent'
    store,groups,space,service,now=make_service(tmp_path,decide)
    service.profiles=lambda:profiles
    target = groups.create_group('g',['alice','claude'],now).id if channel=='GROUP' else str(space.create_post('alice','post',now).id)
    r=service.contact('claude','question',context=CollaborationContext('alice',channel,target,'revocation',None,now))
    events=groups.list_events(target) if channel=='GROUP' else space.list_comments(int(target))
    assert len(events)==1
    assert r['status']=='FAILED' and r['reason']=='PERMISSION_CHANGED'
    assert 'reply_event_id' not in r


def test_space_visibility_revoked_during_decision(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:None)
    post=space.create_post('alice','post',now)
    def decide(*args):
        with store.transaction():
            store.conn.execute("UPDATE space_posts SET visibility='PRIVATE' WHERE id=?",(post.id,))
        return 'blocked reply'
    service.reply_decider=decide
    r=service.contact('claude','question',context=CollaborationContext('alice','SPACE',str(post.id),'private',None,now))
    assert r['status']=='FAILED' and r['reason']=='PERMISSION_CHANGED'
    assert len(space.list_comments(post.id))==1


def test_receipt_migrations_registered(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:None)
    GitHubIssueAdapter(store,lambda **kw:None)
    assert 'collaboration/001-contact-receipts' in store.list_schema_migrations()
    assert 'collaboration/002-issue-write-receipts' in store.list_schema_migrations()


def test_independent_connections_concurrent_contact_claim_is_unknown(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release=Event(),Event()
    def decide(*args):
        entered.set()
        assert release.wait(5)
        return 'single reply'
    store,groups,space,service,now=make_service(tmp_path,decide)
    group=groups.create_group('g',['alice','claude'],now)
    other=SQLiteStore(tmp_path/'roles.db')
    second=RoleCollaborationService(other,GroupRepository(other),SpaceRepository(other),
        profiles=service.profiles,reply_decider=lambda *args:pytest.fail('duplicate execution'))
    c=CollaborationContext('alice','GROUP',group.id,'concurrent',None,now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(service.contact,'claude','question',context=c)
        try:
            assert entered.wait(5)
            duplicate=pool.submit(second.contact,'claude','question',context=c).result(timeout=5)
            assert duplicate['status']=='UNKNOWN'
            assert len(groups.list_events(group.id))==1
        finally:
            release.set()
        receipt=first.result(timeout=5)
    assert receipt['status']=='COMPLETED'
    assert len(groups.list_events(group.id))==2
    assert second.contact('claude','question',context=c)==receipt
    other.close()


def test_independent_connections_concurrent_issue_claim_is_unknown(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    store=SQLiteStore(tmp_path/'issue-concurrent.db')
    def transport(**kwargs):
        entered.set()
        assert release.wait(5)
        return {'html_url':'https://github.com/owner/repo/issues/1'}
    first=GitHubIssueAdapter(store,transport)
    other=SQLiteStore(tmp_path/'issue-concurrent.db')
    second=GitHubIssueAdapter(other,lambda **kwargs:pytest.fail('duplicate external write'))
    args=dict(character_id='claude',request_id='same',repository='owner/repo',title='title',body='body',
        grant=IssueWriteGrant('operator',('owner/repo',),1,'claude'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(first.create,**args)
        try:
            assert entered.wait(5)
            duplicate=pool.submit(second.create,**args).result(timeout=5)
            assert duplicate['status']=='UNKNOWN' and 'url' not in duplicate
        finally:
            release.set()
        receipt=future.result(timeout=5)
    assert receipt['status']=='CREATED'
    assert second.create(**args)==receipt
    other.close()


@pytest.mark.parametrize('channel',['GROUP','SPACE'])
def test_collaboration_experience_survives_restart_only_for_participants(tmp_path,channel):
    from character_memory.runtime.person_context import PersonContextBuilder
    store,groups,space,service,now=make_service(tmp_path,lambda *args:'Different opinion')
    target=groups.create_group('g',['alice','claude'],now).id if channel=='GROUP' else str(space.create_post('alice','post',now).id)
    receipt=service.contact('claude','Public question',context=CollaborationContext('alice',channel,target,'experience',None,now))
    assert receipt['status']=='COMPLETED'
    store.close()
    reopened=SQLiteStore(tmp_path/'roles.db')
    builder=PersonContextBuilder(reopened,SimpleNamespace(recall=lambda *args,**kwargs:[]),'same persona')
    for identity in ['alice','claude']:
        snapshot=builder.build(identity,query='Public question',at=now,observed_projection='PUBLIC')
        facts=snapshot.recent_events
        assert len(facts)==2
        assert {f.content for f in facts}=={'alice → claude: Public question','claude → alice: Different opinion'}
        assert all(f.metadata['channel']==channel and f.metadata['participants']==['alice','claude'] for f in facts)
        assert all(f.metadata['opportunity_id']=='experience' for f in facts)
        assert all(f.event_type.value!='WORLD_OBSERVATION' for f in facts)
        assert {f.metadata['shared_fact_id'] for f in facts}=={receipt['sent_event_id'],receipt['reply_event_id']}
        assert not reopened.list_chat_events(identity)
        assert not reopened.list_memories(identity)
    assert not builder.build('outsider',query='question',at=now).recent_events
    reopened.close()


def test_group_commit_publication_no_duplicate_on_replay_or_decision_failure(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:'reply')
    group=groups.create_group('g',['alice','claude'],now)
    published=[]
    def publish(payload):
        assert not store.conn.in_transaction
        assert any(e.id==payload['id'] for e in groups.list_events(group.id))
        published.append(payload)
    service.publish_group_event=publish
    c=CollaborationContext('alice','GROUP',group.id,'publication',None,now)
    first=service.contact('claude','question',context=c)
    assert [p['actor_id'] for p in published]==['alice','claude']
    assert all(p['conversation_id']==group.id for p in published)
    assert service.contact('claude','question',context=c)==first
    assert len(published)==2
    def fail(*args):
        raise RuntimeError('declined')
    service.reply_decider=fail
    failed=service.contact('claude','next question',context=CollaborationContext('alice','GROUP',group.id,'failed-publication',None,now))
    assert failed['status']=='FAILED'
    assert [p['actor_id'] for p in published]==['alice','claude','alice']
    assert len(store.list_events('alice'))==3


def test_interrupted_recipient_preserves_sender_projection_and_publish_without_replay(tmp_path):
    def interrupted(*args):
        raise KeyboardInterrupt()
    store,groups,space,service,now=make_service(tmp_path,interrupted)
    group=groups.create_group('g',['alice','claude'],now)
    published=[]
    service.publish_group_event=published.append
    c=CollaborationContext('alice','GROUP',group.id,'interrupted',None,now)
    with pytest.raises(KeyboardInterrupt):
        service.contact('claude','already sent',context=c)
    assert len(groups.list_events(group.id))==len(published)==1
    assert len(store.list_events('alice'))==len(store.list_events('claude'))==1
    assert service.contact('claude','already sent',context=c)['status']=='UNKNOWN'
    assert len(published)==1
    assert len(store.list_events('alice'))==1


def test_shared_projection_never_copies_private_sender_events(tmp_path):
    from character_memory.domain.models import Event,EventType
    store,groups,space,service,now=make_service(tmp_path,lambda *args:None)
    store.append_message(Event(character_id='alice',event_type=EventType.USER_MESSAGE,
        event_time=now,content='private-user-secret',metadata={'channel':'DIRECT','conversation_id':'alice:default'}))
    group=groups.create_group('g',['alice','claude'],now)
    service.contact('claude','public only',context=CollaborationContext('alice','GROUP',group.id,'privacy',None,now))
    recipient=store.list_events('claude')
    assert len(recipient)==1 and 'private-user-secret' not in recipient[0].model_dump_json()
    assert recipient[0].metadata['direction']=='RECEIVED'
    assert not store.list_chat_events('claude')


def test_publication_failure_does_not_undo_real_sent_fact_or_trigger_retry(tmp_path):
    store,groups,space,service,now=make_service(tmp_path,lambda *args:None)
    group=groups.create_group('g',['alice','claude'],now)
    attempts=[]
    def fail(payload):
        attempts.append(payload)
        raise RuntimeError('disconnected')
    service.publish_group_event=fail
    c=CollaborationContext('alice','GROUP',group.id,'disconnected',None,now)
    receipt=service.contact('claude','real fact',context=c)
    assert receipt['status']=='SILENT'
    assert len(groups.list_events(group.id))==len(store.list_events('alice'))==1
    assert service.contact('claude','real fact',context=c)==receipt
    assert len(attempts)==1


@pytest.mark.parametrize('channel', ['GROUP', 'SPACE'])
@pytest.mark.parametrize('reply', ['Actual reply', None])
def test_life_collaboration_reads_only_actual_participant_receipts(tmp_path, channel, reply):
    from character_memory.character_life import CharacterLifeReader
    from character_memory.life_conversation import LifeReference, resolve_life_reference
    from character_memory.domain.models import Event, EventType
    store, groups, space, service, now = make_service(tmp_path, lambda *args: reply)
    service.profiles = lambda: [{'id': p} for p in ('alice', 'claude', 'outsider')]
    target = groups.create_group('g', ['alice', 'claude', 'outsider'], now).id if channel == 'GROUP' else str(space.create_post('outsider', 'Public post', now).id)
    receipt = service.contact('claude', 'Actual question', context=CollaborationContext('alice', channel, target, 'life-read', None, now))
    for cid in ('alice', 'claude'):
        store.append_message(Event(character_id=cid, event_type=EventType.USER_MESSAGE, event_time=now, content='Existing Direct', metadata={'conversation_id': cid + ':existing'}))
    kind = 'group' if channel == 'GROUP' else 'comment'
    expected = {receipt['sent_event_id']}
    if reply:
        expected.add(receipt['reply_event_id'])
    store.close()
    store = SQLiteStore(tmp_path / 'roles.db')
    reader = CharacterLifeReader(store)
    before = store.conn.total_changes
    for cid, peer in [('alice', 'claude'), ('claude', 'alice')]:
        records = reader.timeline(cid, channel=channel)['items']
        actual = [i for i in records if i['kind'] == kind]
        assert {i['source']['id'] for i in actual} == expected
        assert len(actual) == len(expected)
        relationships = reader.timeline(cid, channel=channel, view='relationships', peer=peer)['items']
        assert relationships
        assert all(i['participants'] == [{'id': peer, 'name': peer}] for i in relationships)
        assert reader.timeline(cid, channel=channel, view='relationships', peer='outsider')['items'] == []
        incoming = receipt['sent_event_id'] if cid == 'claude' else receipt.get('reply_event_id')
        if incoming:
            ref = resolve_life_reference(store, LifeReference(character_id=cid, key=f'{kind}:{incoming}'))
            actor = 'alice' if cid == 'claude' else 'claude'
            assert f'记录发言者/观察者：{actor}。' in ref['quote']
            expected_target = {'type': 'GROUP', 'id': target} if channel == 'GROUP' else {'type': 'DIRECT', 'id': cid + ':existing'}
            assert ref['conversation'] == expected_target
    assert not reader.timeline('outsider', channel=channel, view='relationships')['items']
    assert not [i for i in reader.timeline('outsider', channel=channel)['items'] if i['kind'] == kind]
    assert store.conn.total_changes == before
    store.close()


@pytest.mark.parametrize('channel', ['GROUP', 'SPACE'])
@pytest.mark.parametrize('field,value', [('channel', 'DIRECT'), ('channel_id', 'wrong'), ('shared_fact_id', 999), ('actor_id', 'outsider'), ('recipient_id', 'outsider'), ('direction', 'SENT'), ('malformed', None)])
def test_life_rejects_mismatched_collaboration_proof(tmp_path, channel, field, value):
    import json
    from fastapi import HTTPException
    from character_memory.character_life import CharacterLifeReader
    from character_memory.life_conversation import LifeReference, resolve_life_reference
    store, groups, space, service, now = make_service(tmp_path, lambda *args: None)
    target = groups.create_group('g', ['alice', 'claude'], now).id if channel == 'GROUP' else str(space.create_post('alice', 'post', now).id)
    receipt = service.contact('claude', 'Question', context=CollaborationContext('alice', channel, target, 'proof', None, now))
    row = store.conn.execute("SELECT id,metadata_json FROM events WHERE character_id='claude' AND event_type='ACTION'").fetchone()
    metadata = json.loads(row['metadata_json'])
    metadata[field] = value
    with store.transaction():
        store.conn.execute('UPDATE events SET metadata_json=? WHERE id=?', ['{invalid' if field == 'malformed' else json.dumps(metadata), row['id']])
    assert not CharacterLifeReader(store).timeline('claude', channel=channel)['items']
    key = ('group:' if channel == 'GROUP' else 'comment:') + str(receipt['sent_event_id'])
    with pytest.raises(HTTPException) as denied:
        resolve_life_reference(store, LifeReference(character_id='claude', key=key))
    assert denied.value.status_code == 404
    store.close()


@pytest.mark.parametrize('channel', ['GROUP', 'SPACE'])
def test_collaboration_life_reference_retains_current_channel_guards(tmp_path, channel):
    from fastapi import HTTPException
    from character_memory.life_conversation import LifeReference, resolve_life_reference
    store, groups, space, service, now = make_service(tmp_path, lambda *args: None)
    target = groups.create_group('g', ['alice', 'claude'], now).id if channel == 'GROUP' else str(space.create_post('alice', 'post', now).id)
    receipt = service.contact('claude', 'Question', context=CollaborationContext('alice', channel, target, 'guarded-ref', None, now))
    key = ('group:' if channel == 'GROUP' else 'comment:') + str(receipt['sent_event_id'])
    with store.transaction():
        if channel == 'GROUP':
            store.conn.execute("DELETE FROM conversation_members WHERE conversation_id=? AND actor_id='claude'", [target])
        else:
            store.conn.execute("UPDATE space_posts SET visibility='PRIVATE' WHERE id=?", [int(target)])
    with pytest.raises(HTTPException) as denied:
        resolve_life_reference(store, LifeReference(character_id='claude', key=key))
    assert denied.value.status_code == (404 if channel == 'GROUP' else 409)
    store.close()
