from datetime import datetime, timezone
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
