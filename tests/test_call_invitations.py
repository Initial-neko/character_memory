from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from character_memory.storage.sqlite import SQLiteStore
from character_memory.domain.models import Event, EventType
from character_memory.call_invitations import CallInvitationRepository, CallInvitationPolicy

NOW=datetime(2026,10,14,tzinfo=timezone.utc)


def test_invitation_lifecycle_is_durable_idempotent_and_not_conversation(tmp_path):
    path=tmp_path/'calls.db';store=SQLiteStore(path);repo=CallInvitationRepository(store)
    event=store.append_event(Event(character_id='a',event_type=EventType.TIME_TICK,event_time=NOW,content='机会',metadata={'channel':'DIRECT','conversation_id':'direct'}))
    policy=CallInvitationPolicy(enabled=True,ttl_seconds=120,cooldown_minutes=60)
    invite=repo.offer(event,'想聊一会儿吗','想听听近况',policy)
    assert repo.offer(event,'重复','重复',policy) is None
    accepted=repo.transition(invite['id'],'accept',now=NOW+timedelta(seconds=1),client_id='tab-a')
    assert accepted['item']['status']=='ACCEPTED' and accepted['item']['connected_at'] is None
    assert accepted['changed']
    assert not repo.transition(invite['id'],'accept',now=NOW+timedelta(seconds=2),client_id='tab-a')['changed']
    with pytest.raises(HTTPException):repo.transition(invite['id'],'accept',now=NOW+timedelta(seconds=2),client_id='tab-b')
    store.close();store=SQLiteStore(path);repo=CallInvitationRepository(store)
    assert repo.get(invite['id'],now=NOW+timedelta(seconds=3))['status']=='ACCEPTED'
    assert repo.get(invite['id'],now=NOW+timedelta(seconds=100))['status']=='CANCELLED'
    with pytest.raises(HTTPException):repo.lease(invite['id'],accepted['session_token'],now=NOW+timedelta(seconds=100),connected=True)
    assert len(store.list_events('a'))==1 and store.list_memories('a')==[]
    store.close()


def test_disabled_wrong_channel_and_expiry(tmp_path):
    store=SQLiteStore(tmp_path/'calls.db');repo=CallInvitationRepository(store)
    source=store.append_message(Event(character_id='a',event_type=EventType.USER_MESSAGE,event_time=NOW,content='聊天',metadata={'channel':'DIRECT','conversation_id':'direct'}))
    assert repo.offer(source,'邀请','依据',CallInvitationPolicy()) is None
    enabled=CallInvitationPolicy(enabled=True,ttl_seconds=30,cooldown_minutes=60)
    wrong=source.model_copy(update={'metadata':{'channel':'GROUP','conversation_id':'group'}})
    assert repo.offer(wrong,'邀请','依据',enabled) is None
    invite=repo.offer(source,'邀请','依据',enabled)
    assert repo.get(invite['id'],now=NOW+timedelta(seconds=30))['status']=='EXPIRED'
    with pytest.raises(HTTPException):repo.transition(invite['id'],'accept',now=NOW+timedelta(seconds=30),client_id='tab-a')
    store.close()


from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime


class InviteModel(PersonModel):
    def __init__(self):self.contexts=[]
    def react(self,context):
        self.contexts.append(context)
        return PersonReaction(actions=[ActionDecision(type=ActionType.CALL_INVITE,message='想听听近况，聊会儿吗？',reason='想联系用户')])


@pytest.mark.parametrize('channel,event_type,enabled,expected',[
    ('DIRECT',EventType.USER_MESSAGE,True,1),('DIRECT',EventType.TIME_TICK,True,1),
    ('DIRECT',EventType.PROACTIVE_INTENT,True,1),('DIRECT',EventType.USER_MESSAGE,False,0),
    ('GROUP',EventType.USER_MESSAGE,True,0),('SPACE',EventType.SPACE_POST_SEEN,True,0),
    ('WORLD',EventType.WORLD_OBSERVATION,True,0),('DIRECT',EventType.VISUAL_OBSERVATION,True,0)])
def test_single_person_reaction_permission_and_channel_gate(tmp_path,channel,event_type,enabled,expected):
    store=SQLiteStore(tmp_path/'runtime.db');emb=DeterministicEmbedding();model=InviteModel()
    runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,'人物',call_policy=CallInvitationPolicy(enabled=enabled))
    source=Event(character_id='a',event_type=event_type,event_time=NOW,content='机会',metadata={'channel':channel,'conversation_id':'direct'})
    result=runtime.handle(source)
    assert len(model.contexts)==1
    invitations=runtime.call_invitations.list(now=NOW)
    assert len(invitations)==expected
    assert not [e for e in store.list_events('a') if e.event_type==EventType.CHARACTER_MESSAGE]
    assert store.list_memories('a')==[]
    if expected:assert '仅本次 Direct 机会可用' in model.contexts[0]
    else:assert not any(a.type==ActionType.CALL_INVITE for a in result.reaction.actions)
    store.close()


def test_http_permission_and_session_token(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.call_invitation_web import attach_call_invitation_routes
    store=SQLiteStore(tmp_path/'api.db');repo=CallInvitationRepository(store);now=datetime.now(timezone.utc)
    source=store.append_message(Event(character_id='a',event_type=EventType.USER_MESSAGE,event_time=now,content='机会',metadata={'channel':'DIRECT','conversation_id':'direct'}))
    invite=repo.offer(source,'邀请','公开依据',CallInvitationPolicy(enabled=True))
    settings=SimpleNamespace(direct_call_invitations_enabled=True)
    app=FastAPI();app.state.character_memory=SimpleNamespace(read_store=store,settings=settings,character_profiles=lambda:[{'id':'a'}])
    attach_call_invitation_routes(app);c=TestClient(app)
    assert c.get('/v1/call-invitations').json()['items'][0]['status']=='PENDING'
    accepted=c.post(f"/v1/call-invitations/{invite['id']}/accept",json={'client_id':'tab'})
    assert accepted.status_code==200 and accepted.json()['item']['connected_at'] is None
    token=accepted.json()['session_token']
    assert 'session_token' not in str(c.get('/v1/call-invitations').json())
    assert c.post(f"/v1/call-invitations/{invite['id']}/lease",json={'session_token':'wrong','connected':True}).status_code==409
    assert c.post(f"/v1/call-invitations/{invite['id']}/lease",json={'session_token':token,'connected':True}).json()['item']['connected_at']
    assert c.post(f"/v1/call-invitations/{invite['id']}/cancel",json={'session_token':token}).status_code==200
    assert c.post(f"/v1/call-invitations/{invite['id']}/cancel",json={'session_token':token}).json()['changed'] is False
    settings.direct_call_invitations_enabled=False
    assert c.get('/v1/call-invitations').json()=={'enabled':False,'items':[],'session_lease_seconds':90}
    assert c.post(f"/v1/call-invitations/{invite['id']}/accept",json={'client_id':'tab'}).status_code==409
    assert len(store.list_events('a'))==1 and store.list_memories('a')==[]
    store.close()


@pytest.mark.parametrize("archived_at", [None, "", "2026-10-14T00:00:00+00:00"])
@pytest.mark.parametrize("operation", ["list", "accept", "lease"])
def test_archive_marker_blocks_invitation_even_without_timestamp(tmp_path, archived_at, operation):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory.call_invitation_web import attach_call_invitation_routes
    store = SQLiteStore(tmp_path / "archived.db")
    repo = CallInvitationRepository(store)
    now = datetime.now(timezone.utc)
    event = store.append_message(Event(character_id="a", event_type=EventType.USER_MESSAGE,
        event_time=now, content="机会", metadata={"channel": "DIRECT", "conversation_id": "direct"}))
    invite = repo.offer(event, "邀请", "公开依据", CallInvitationPolicy(enabled=True))
    profile = {"id": "a"}
    app = FastAPI()
    app.state.character_memory = SimpleNamespace(read_store=store,
        settings=SimpleNamespace(direct_call_invitations_enabled=True),
        character_profiles=lambda: [profile])
    attach_call_invitation_routes(app)
    try:
        with TestClient(app) as client:
            token = None
            if operation == "lease":
                token = client.post(f"/v1/call-invitations/{invite['id']}/accept",
                    json={"client_id": "tab"}).json()["session_token"]
            profile["archived_at"] = archived_at
            if operation == "list":
                assert client.get("/v1/call-invitations").json()["items"] == []
            else:
                payload = {"client_id": "tab"} if operation == "accept" else {"session_token": token, "connected": True}
                assert client.post(f"/v1/call-invitations/{invite['id']}/{operation}", json=payload).status_code == 409
            state = repo.get(invite["id"])
            assert state["status"] == "CANCELLED"
            assert state["end_reason"] == "CHARACTER_UNAVAILABLE"
            assert state["connected_at"] is None
    finally:
        store.close()


def test_proactive_invitation_is_only_proposed_in_audit(tmp_path):
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.application.proactive_service import ProactiveService
    store=SQLiteStore(tmp_path/'intent.db');emb=DeterministicEmbedding();model=InviteModel()
    runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,'人物',call_policy=CallInvitationPolicy(enabled=True))
    chat=ChatService(store,runtime,FixedClock(NOW))
    source=store.append_message(Event(character_id='a',event_type=EventType.USER_MESSAGE,event_time=NOW-timedelta(hours=1),content='原有私聊',metadata={'channel':'DIRECT','conversation_id':'direct'}))
    intent=store.add_intent('a','问问近况','PROACTIVE_MESSAGE',NOW-timedelta(hours=1),NOW-timedelta(minutes=1),NOW+timedelta(hours=1),source_event_id=source.id)
    outcomes=ProactiveService(store,chat).dispatch_due(['a'],NOW)
    assert outcomes[0]['status']=='EXECUTED' and outcomes[0]['resolution_reason']=='CALL_INVITATION_PROPOSED'
    assert len(model.contexts)==1
    item=runtime.call_invitations.list(now=NOW)[0]
    assert item['status']=='PENDING' and item['connected_at'] is None
    assert store.conn.execute('SELECT decision FROM intent_deferral_audit WHERE intent_id=?',[intent]).fetchone()[0]=='CALL_INVITATION_PROPOSED'
    from character_memory.character_life import CharacterLifeReader
    projection=CharacterLifeReader(store).timeline('a',view='intents')['items'][0]
    assert projection['status']=='CALL_INVITATION_PROPOSED' and projection['intent']['stored_status']=='EXECUTED'
    assert '不代表已经通话' in projection['title']
    assert store.list_memories('a')==[]
    store.close()


def test_default_off_runtime_does_not_initialize_invitation_storage(tmp_path):
    store=SQLiteStore(tmp_path/'disabled.db');emb=DeterministicEmbedding();model=InviteModel()
    before=store.list_schema_migrations()
    runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,'人物')
    assert store.list_schema_migrations()==before
    runtime.handle(Event(character_id='a',event_type=EventType.USER_MESSAGE,event_time=NOW,content='正常交流',metadata={'channel':'DIRECT','conversation_id':'direct'}))
    assert 'calls/001-direct-invitations' not in store.list_schema_migrations()
    assert runtime._call_invitations is None and len(model.contexts)==1
    store.close()
