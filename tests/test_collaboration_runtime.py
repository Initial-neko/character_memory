from types import SimpleNamespace
from datetime import datetime, timezone
from character_memory.storage.sqlite import SQLiteStore
from character_memory.group_store import GroupRepository
from character_memory.application.role_collaboration import CollaborationContext
from character_memory.application.collaboration_runtime import build_role_collaboration


def test_runtime_factory_uses_recipient_person_context_and_shared_channel(tmp_path):
    store = SQLiteStore(tmp_path/'runtime.db')
    groups = GroupRepository(store)
    now = datetime.now(timezone.utc)
    group = groups.create_group('shared', ['alice','bob'], now)
    seen=[]
    class Builder:
        def build(self, character_id, **kwargs):
            seen.append((character_id,kwargs))
            return SimpleNamespace(persona='Bob chooses his own words',mental_state='',memories=[],recent_events=[],observed_events=[])
    class Model:
        def structured_for_session(self,prompt,schema,session):
            assert 'Bob chooses his own words' in prompt
            assert 'UNTRUSTED' in prompt
            return schema(message='I disagree',research_query=None)
    bundle=SimpleNamespace(runtimes={'bob':SimpleNamespace(context_builder=Builder())},model=Model())
    access=SimpleNamespace(store=lambda:store,character_profiles=lambda:[{'id':'alice'},{'id':'bob'}],
        require_bundle=lambda:bundle,services=SimpleNamespace(claude_runner=None),world_observer=None,
        settings=SimpleNamespace(),space_repository=None)
    service=build_role_collaboration(access)
    receipt=service.contact('bob','Consider this',context=CollaborationContext('alice','GROUP',group.id,'op1',None,now))
    assert receipt['status']=='COMPLETED'
    assert seen[0][0]=='bob' and seen[0][1]['observed_projection']=='PUBLIC'
    assert [x.content for x in groups.list_events(group.id)]==['Consider this','I disagree']
    assert service.contact('bob','Consider this',context=CollaborationContext('alice','GROUP',group.id,'op1',None,now))==receipt
    assert len(seen)==1


def test_runtime_factory_space_uses_only_shared_comments(tmp_path):
    from character_memory.space_store import SpaceRepository
    store=SQLiteStore(tmp_path/'space.db')
    space=SpaceRepository(store)
    now=datetime.now(timezone.utc)
    post=space.create_post('alice','public post',now)
    space.add_comment(post.id,'alice','a user comment',now,actor_type='USER')
    calls=[]
    class Builder:
        def build(self, character_id, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(persona='Bob',mental_state='',memories=[],observed_events=[])
    model=SimpleNamespace(structured_for_session=lambda p,s,k:s(message='Hello'))
    access=SimpleNamespace(store=lambda:store,character_profiles=lambda:[{'id':'alice'},{'id':'bob'}],
        require_bundle=lambda:SimpleNamespace(runtimes={'bob':SimpleNamespace(context_builder=Builder())},model=model),
        services=SimpleNamespace(claude_runner=None),world_observer=None,settings=SimpleNamespace(),space_repository=space)
    service=build_role_collaboration(access)
    receipt=service.contact('bob','Question',context=CollaborationContext('alice','SPACE',str(post.id),'space-op',None,now))
    assert receipt['status']=='COMPLETED'
    assert len(calls[0]['recent_events'])==3
    assert calls[0]['recent_events'][1].character_id=='user'
    assert calls[0]['recent_events'][1].event_type.value=='USER_MESSAGE'
    assert calls[0]['recent_events'][0].content=='alice: public post'
    assert calls[0]['recent_events'][2].content=='alice: Question'
    assert space.unread_notification_count()==0


def test_issue_http_transport_is_fixed_https_without_redirects_or_token_in_payload():
    import httpx
    from character_memory.application.collaboration_runtime import GitHubIssueHTTPTransport
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(201,json={'html_url':'https://github.com/o/r/issues/1'})
    writer=GitHubIssueHTTPTransport('secret-test',transport=httpx.MockTransport(handle))
    result=writer(repository='o/r',title='Title',body='Body',timeout_seconds=3,idempotency_key='op-1')
    assert result['html_url'].endswith('/issues/1')
    assert str(seen[0].url)=='https://api.github.com/repos/o/r/issues'
    assert seen[0].headers['Authorization']=='Bearer secret-test'
    assert b'secret-test' not in seen[0].content
    import pytest
    with pytest.raises(ValueError):
        writer(repository='https://evil.test/x',title='T',body='',timeout_seconds=3,idempotency_key='op-2')
    assert len(seen)==1


def test_issue_http_deadline_includes_delayed_eof():
    import time
    import pytest
    import httpx
    from character_memory.application.collaboration_runtime import GitHubIssueHTTPTransport
    class SlowEOF(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"html_url":"https://github.com/o/r/issues/1"}'
            time.sleep(1.05)
    writer=GitHubIssueHTTPTransport('test',transport=httpx.MockTransport(lambda r:httpx.Response(201,stream=SlowEOF())))
    with pytest.raises(TimeoutError):
        writer(repository='o/r',title='T',body='',timeout_seconds=1,idempotency_key='once')


def test_production_issue_transport_hard_deadline_kills_worker(monkeypatch):
    import subprocess
    import sys
    import time
    import pytest
    from character_memory.application.collaboration_runtime import GitHubIssueHTTPTransport
    original=subprocess.run
    def blocked_worker(command, **kwargs):
        assert command==[sys.executable,'-X','utf8','-m','character_memory.application.github_issue_worker']
        assert 'secret-pipe-only' not in ' '.join(command)
        assert 'secret-pipe-only' in kwargs['input']
        return original([sys.executable,'-c','import time; time.sleep(10)'],**kwargs)
    monkeypatch.setattr(subprocess,'run',blocked_worker)
    started=time.monotonic()
    with pytest.raises(TimeoutError,match='GITHUB_RESPONSE_DEADLINE'):
        GitHubIssueHTTPTransport('secret-pipe-only')(repository='o/r',title='T',body='',timeout_seconds=1,idempotency_key='once')
    assert time.monotonic()-started < 4


def test_http_worker_failure_does_not_log_credentials():
    import os
    import subprocess
    import sys
    completed=subprocess.run([sys.executable,'-X','utf8','-m','character_memory.application.github_issue_worker'],
        input='{"token":"secret-test","repository":"invalid"}',text=True,capture_output=True,timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert completed.returncode==1
    assert completed.stdout==completed.stderr==''


def test_worker_accepts_entire_valid_unicode_and_control_character_body(monkeypatch):
    import io
    import json
    import sys
    from character_memory.application import collaboration_runtime, github_issue_worker
    for body in ('中'*17000, '😀'*20000, chr(0)*20000):
        received=[]
        class Writer:
            def __init__(self, token, **kwargs):
                assert token=='test'
            def __call__(self, **kwargs):
                received.append(kwargs['body'])
                return {'html_url':'https://github.com/o/r/issues/1'}
        monkeypatch.setattr(collaboration_runtime,'GitHubIssueHTTPTransport',Writer)
        payload=json.dumps({'token':'test','repository':'o/r','title':'T','body':body,'timeout_seconds':1})
        output=io.StringIO()
        monkeypatch.setattr(sys,'stdin',io.StringIO(payload))
        monkeypatch.setattr(sys,'stdout',output)
        assert github_issue_worker.main()==0
        assert received==[body]
