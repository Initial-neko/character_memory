import os
import socket
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
import pytest
from character_memory.domain.models import Event, EventType
from character_memory.call_invitations import CallInvitationPolicy
from test_call_invitations import InviteModel

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]


@pytest.fixture
def call_server(tmp_path):
    import uvicorn
    from character_memory.api import create_api
    from character_memory.async_web import attach_async_routes
    from character_memory.group_web import attach_group_routes
    from character_memory.history_web import attach_history_routes
    from character_memory.call_invitation_web import attach_call_invitation_routes
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    root=Path(__file__).parents[1];config=tmp_path/'config.yaml'
    config.write_text(f'api_key: ""\nembedding_provider: deterministic\ndirect_call_invitations_enabled: true\ndb_path: "{(tmp_path/"calls.db").as_posix()}"\npersona_path: "{(root/"personas/rin/persona.yaml").as_posix()}"\n',encoding='utf-8')
    app=create_api(str(config));attach_group_routes(app,str(config));attach_async_routes(app);attach_history_routes(app);attach_call_invitation_routes(app)
    access=app.state.character_memory;store=access.store();character=access.character_profiles()[0]['id'];emb=DeterministicEmbedding();model=InviteModel()
    runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,'人物',call_policy=CallInvitationPolicy(enabled=True))
    runtime.handle(Event(character_id=character,event_type=EventType.TIME_TICK,event_time=datetime.now(timezone.utc),content='已有Direct机会',metadata={'channel':'DIRECT','conversation_id':'original-direct'}))
    invitation=runtime.call_invitations.list()[0]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        yield f'http://127.0.0.1:{port}',runtime.call_invitations,invitation,model,store,access.stream_hub
    finally:
        access.stream_hub.close();access.reaction_scheduler.close();server.should_exit=True;thread.join(10)
        assert not thread.is_alive()


def prepare(page,base,mode='ok'):
    page.route('http://127.0.0.1:8001/health',lambda r:r.fulfill(json={'asr':{'ready':True},'tts':{'ready':True}}))
    page.goto(base)
    from playwright.sync_api import expect
    expect(page.get_by_role('button',name='接受并开启语音')).to_be_visible()
    page.evaluate("""mode=>{
      window.MIC_REQUESTS=0;window.MIC_STOPS=0;
      window.CM.mediaAudio.requestMicrophone=async()=>{
        window.MIC_REQUESTS++;
        if(mode==='denied')throw new Error('permission denied');
        if(mode==='delayed')await new Promise(resolve=>window.RELEASE_PERMISSION=resolve);
        return {getTracks:()=>[{stop:()=>window.MIC_STOPS++}]};
      };
      window.CM.mediaAudio.createCapture=()=>({
        context:{sampleRate:48000,destination:{},close:async()=>{},createScriptProcessor:()=>({connect:()=>{},disconnect:()=>{}})},
        source:{connect:()=>{},disconnect:()=>{}}
      });
    }""",mode)


def test_frontend_decline_does_not_open_microphone(page,call_server):
    base,repo,item,model,store,hub=call_server;prepare(page,base)
    assert page.evaluate('window.MIC_REQUESTS')==0
    page.get_by_role('button',name='拒绝',exact=True).click()
    page.wait_for_function("!document.querySelector('section[aria-label=人物来电邀请] button')")
    assert repo.get(item['id'])['status']=='DECLINED'
    assert page.evaluate('window.MIC_REQUESTS')==0
    from character_memory.application.async_conversation import direct_channel
    target=page.evaluate('[window.CM.state.characterId,window.CM.conversationIdFor(window.CM.state.characterId)]')
    with page.expect_response('**/v1/call-invitations'):
        hub.publish(direct_channel(*target),'call_invitation',{'id':item['id']})
    assert page.evaluate('window.MIC_REQUESTS')==0
    assert repo.get(item['id'])['status']=='DECLINED'
    assert len(model.contexts)==1 and store.list_memories(item['character_id'])==[]


def test_frontend_accept_hangup_and_no_automatic_reconnect(page,call_server):
    from playwright.sync_api import expect
    base,repo,item,model,store,hub=call_server;prepare(page,base)
    assert page.evaluate('window.MIC_REQUESTS')==0
    page.get_by_role('button',name='接受并开启语音').click()
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text('应用内语音已开启')
    assert page.evaluate('window.MIC_REQUESTS')==1
    assert repo.get(item['id'])['connected_at'] is not None
    page.evaluate('window.CM.features.voice.stop()')
    page.wait_for_function("!window.CM.features.voice.state.active")
    for _ in range(50):
        if repo.get(item['id'])['status']=='CANCELLED':break
        time.sleep(.02)
    assert repo.get(item['id'])['status']=='CANCELLED'
    assert page.evaluate('window.MIC_STOPS')==1
    page.reload()
    page.wait_for_function('window.CM?.features?.callInvitations')
    assert page.evaluate('window.CM.features.voice.state.active') is False
    assert len(model.contexts)==1 and store.list_memories(item['character_id'])==[]
    assert not any(e.event_type==EventType.CHARACTER_MESSAGE for e in store.list_events(item['character_id']))


def test_frontend_microphone_denied_is_not_successful_call(page,call_server):
    from playwright.sync_api import expect
    base,repo,item,model,store,hub=call_server;prepare(page,base,'denied')
    page.get_by_role('button',name='接受并开启语音').click()
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text('麦克风未开启')
    for _ in range(50):
        if repo.get(item['id'])['status']=='CANCELLED':break
        time.sleep(.02)
    record=repo.get(item['id'])
    assert record['status']=='CANCELLED' and record['connected_at'] is None
    assert page.evaluate('window.CM.features.voice.state.active') is False
    assert store.list_memories(item['character_id'])==[]


def test_frontend_permission_arrives_after_expired_lease(page,call_server):
    from playwright.sync_api import expect
    base,repo,item,model,store,hub=call_server;prepare(page,base,'delayed')
    page.get_by_role('button',name='接受并开启语音').click()
    page.wait_for_function('Boolean(window.RELEASE_PERMISSION)')
    with store.transaction():
        store.conn.execute('UPDATE call_invitations SET lease_until_epoch=0 WHERE id=?',[item['id']])
    assert repo.get(item['id'])['status']=='CANCELLED'
    page.evaluate('window.RELEASE_PERMISSION()')
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text('未接通')
    assert repo.get(item['id'])['connected_at'] is None
    assert page.evaluate('window.CM.features.voice.state.active') is False
    assert page.evaluate('window.MIC_STOPS')==1
    assert store.list_memories(item['character_id'])==[]


def test_browser_restart_does_not_auto_accept_owned_session(page,call_server):
    from playwright.sync_api import expect
    base,repo,item,model,store,hub=call_server;prepare(page,base)
    repo.transition(item['id'],'accept',client_id='other-browser-before-restart')
    page.reload()
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text('本页不会自动接通')
    assert page.evaluate('window.CM.features.voice.state.active') is False
    assert repo.get(item['id'])['connected_at'] is None
    assert len(model.contexts)==1
