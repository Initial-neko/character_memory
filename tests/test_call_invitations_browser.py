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
from test_life_conversation import isolated_app

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]


@pytest.fixture
def call_server(isolated_app,monkeypatch):
    import socket,threading,time,uvicorn
    from character_memory.call_invitation_web import attach_call_invitation_routes
    from character_memory.call_invitations import CallInvitationPolicy
    from character_memory.runtime.person_runtime import PersonRuntime
    from character_memory.memory.embedding import DeterministicEmbedding
    from character_memory.memory.recall import VectorRecall
    from character_memory.domain.models import Event,EventType
    from test_call_invitations import InviteModel
    app=isolated_app;access=app.state.character_memory
    access.settings.direct_call_invitations_enabled=True
    attach_call_invitation_routes(app)
    store=access.store();profiles=access.character_profiles();character=profiles[0]['id']
    assert len(profiles)==2 and all(not p.get('archived') for p in profiles)
    def forbid(): raise AssertionError('No real model initialization allowed')
    monkeypatch.setattr(access,'require_bundle',forbid)
    emb=DeterministicEmbedding();model=InviteModel()
    runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,'人物',call_policy=CallInvitationPolicy(enabled=True))
    runtime.handle(Event(character_id=character,event_type=EventType.TIME_TICK,event_time=datetime.now(timezone.utc),content='隔离Direct机会',metadata={'channel':'DIRECT','conversation_id':'original-direct'}))
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
        access.stream_hub.close()
        server.should_exit=True;thread.join(10)
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


import json,hashlib,subprocess

@pytest.mark.parametrize('width,height',[(1440,900),(390,844)])
def test_real_call_invitation_pending_and_denied(page,call_server,width,height):
    from playwright.sync_api import expect
    base,repo,item,model,store,hub=call_server
    root=next(p for p in Path(__file__).resolve().parents if (p/'pyproject.toml').is_file())
    output=Path(os.getenv('CORE_VISUAL_EVIDENCE_DIR',str(root/'artifacts/core-visual-evidence')))/'call-invitation-control'
    output.mkdir(parents=True,exist_ok=True)
    page.set_viewport_size({'width':width,'height':height})
    prepare(page,base,'denied')
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_be_visible()
    captures=[]
    def capture(state):
        page.evaluate('() => document.fonts.ready')
        for full in [False,True]:
            name=f'call-{width}x{height}-{state}'+('-full' if full else '')+'.png'
            target=output/name
            page.screenshot(path=str(target),full_page=full,animations='disabled')
            captures.append({'state':state,'path':name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'at':datetime.now(timezone.utc).isoformat(),'full_page':full,
                'horizontal_overflow':page.evaluate('document.documentElement.scrollWidth>innerWidth'),
                'invitation_bbox':page.locator('section[aria-label="人物来电邀请"]').bounding_box()})
    assert page.evaluate('window.MIC_REQUESTS')==0
    capture('pending')
    pending_cancellations=[]
    cancel_url=f"{base}/v1/call-invitations/{item['id']}/cancel"
    page.route(cancel_url, lambda route: pending_cancellations.append(route))
    with page.expect_request(cancel_url):
        page.get_by_role('button',name='接受并开启语音').click()
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text('麦克风未开启')
    # Permission feedback can precede the async voiceStopped cleanup response.
    # Keep that response in flight deliberately; the hint is not DB evidence.
    assert repo.get(item['id'])['status']=='ACCEPTED'
    assert len(pending_cancellations)==1
    with page.expect_response(cancel_url) as cancelled:
        pending_cancellations[0].continue_()
    assert cancelled.value.status==200
    assert repo.get(item['id'])['status']=='CANCELLED'
    assert repo.get(item['id'])['connected_at'] is None
    assert page.evaluate('window.CM.features.voice.state.active') is False
    capture('permission-denied')
    sources={}
    for name in ['index.html','call_invitations.js','voice.js','frontend_v2.css','call_stage.css']:
        response=page.request.get(base+('/' if name=='index.html' else '/static/'+name))
        assert response.ok
        payload=response.body().replace(b'\r\n',b'\n')
        committed=subprocess.check_output(['git','show',f'HEAD:src/character_memory/web/{name}'],cwd=root).replace(b'\r\n',b'\n')
        assert payload==committed,name
        sources[name]=hashlib.sha256(payload).hexdigest()
    assert len(model.contexts)==1 and store.list_memories(item['character_id'])==[]
    data={'checkout_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'formal_runtime_sha':None,
        'browser':page.context.browser.version,'viewport':{'width':width,'height':height},'captures':captures,'served_sha256':sources,
        'invitation_status':repo.get(item['id'])['status'],'connected_at':repo.get(item['id'])['connected_at'],
        'mic_requests':page.evaluate('window.MIC_REQUESTS'),'scope':'Real uvicorn/Core/SQLite and production renderer; temporary DB/config, existing InviteModel single fake reaction. Media health readiness mocked and microphone permission denial injected through existing browser fixture; no actual mic/ASR/TTS/network/model provider. No real call completion.',
        'reference':'#294 excludes calls; no approved call/pending/error/mobile reference confirmed','visual_acceptance':'NOT_ASSESSED','human_visual_signoff':'PENDING'}
    (output/f'call-{width}x{height}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')



class MutedInvitationLeaseExpired(AssertionError):
    """Only the fully observed muted-active lease failure is expected."""


@pytest.mark.parametrize("muted", [False, True], ids=["live-mic-control", "muted-active-call"])
def test_call_invitation_muted_lease_continuity(page, call_server, monkeypatch, muted):
    import hashlib
    import json
    import subprocess
    from datetime import timedelta
    from urllib.parse import urlsplit
    from playwright.sync_api import expect
    import character_memory.call_invitations as calls
    from character_memory.domain.models import EventType

    base, repo, item, model, store, hub = call_server
    root = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
    output = Path(os.getenv("CORE_VISUAL_EVIDENCE_DIR", "artifacts/core-visual-evidence")) / "call-muted-lease"
    output.mkdir(parents=True, exist_ok=True)
    instant = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant[0] if tz else instant[0].replace(tzinfo=None)

    monkeypatch.setattr(calls, "datetime", Clock)
    responses = []

    def observe(response):
        path = urlsplit(response.url).path
        if not path.startswith("/v1/call-invitations"):
            return
        request = response.request
        # Deliberately discard request bodies, IDs and all concrete credentials.
        payload = json.loads(request.post_data or "{}")
        responses.append({
            "method": request.method,
            "path": path.replace(item["id"], "{id}"),
            "status": response.status,
            "token_present": bool(payload.get("session_token")),
            "connected": payload.get("connected") is True,
        })

    page.on("response", observe)
    page.add_init_script('''(() => {
      const original = window.setInterval;
      window.CALL_LEASE_TEST_TICKS = [];
      window.setInterval = (fn, delay, ...args) => {
        if (delay === 30000 && String(fn).includes('post(current.id,"lease"')) {
          window.CALL_LEASE_TEST_TICKS.push(fn); return 987654;
        }
        return original(fn, delay, ...args);
      };
    })();''')
    page.set_viewport_size({"width": 1440, "height": 900})
    prepare(page, base)
    page.get_by_role("button", name="接受并开启语音").click()
    expect(page.locator('section[aria-label="人物来电邀请"]')).to_contain_text("应用内语音已开启")
    assert page.evaluate("window.CALL_LEASE_TEST_TICKS.length") == 1
    suffix = "muted" if muted else "live-mic"
    captures = []

    def capture(state):
        target = output / f"{suffix}-{state}.png"
        page.screenshot(path=str(target), full_page=False, animations="disabled")
        captures.append({"state": state, "file": target.name, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})

    def snapshot(seconds):
        row = repo.get(item["id"])
        return {
            "elapsed_seconds": seconds,
            "sqlite": {key: row[key] for key in (
                "status", "accepted_at", "connected_at", "ended_at", "end_reason", "updated_at")},
            "voice": page.evaluate("({active:CM.features.voice.state.active,mic:CM.features.voice.state.micActive})"),
        }

    capture("connected")
    if muted:
        page.locator("#voiceMicButton").click()
        page.wait_for_function("CM.features.voice.state.active && !CM.features.voice.state.micActive")
        capture("muted-active")
    initial = snapshot(0)
    assert initial["voice"] == {"active": True, "mic": not muted}
    assert initial["sqlite"]["status"] == "ACCEPTED" and initial["sqlite"]["connected_at"]
    snapshots = []
    for elapsed in (30, 60, 90):
        instant[0] += timedelta(seconds=30)
        with page.expect_response(lambda r: "/v1/call-invitations" in r.url and
                                  (r.request.method == "GET" or r.url.endswith("/lease"))) as response:
            page.evaluate("window.CALL_LEASE_TEST_TICKS[0]()")
        assert response.value.status == 200
        row = repo.get(item["id"])
        if row["status"] == "CANCELLED":
            page.wait_for_function("!CM.features.voice.state.active")
        snapshots.append(snapshot(elapsed))
    capture("after-90-seconds")
    assert len(model.contexts) == 1 and store.list_memories(item["character_id"]) == []
    assert not any(e.event_type == EventType.CHARACTER_MESSAGE for e in store.list_events(item["character_id"]))
    evidence = {
        "checkout_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "muted": muted, "viewport": {"width": 1440, "height": 900},
        "browser": page.context.browser.version,
        "initial": initial, "snapshots": snapshots, "http": responses, "captures": captures,
        "model_calls": len(model.contexts), "memories": 0,
        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in (
            "src/character_memory/web/call_invitations.js", "src/character_memory/web/voice.js",
            "src/character_memory/call_invitations.py")},
        "scope": "Real Chromium/HTTP/SQLite, isolated fixture and fake microphone; controlled repository clock and original 30-second callback. No real mic/ASR/TTS/provider or wall-clock timing validation. Screenshots are evidence, not approved design or human visual acceptance.",
    }
    (output / f"{suffix}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf8")
    renewal = [r for r in responses if r["path"].endswith("/lease") and not r["connected"]]
    known_failure = (
        muted and not renewal
        and all(s["sqlite"]["status"] == "ACCEPTED" and s["voice"] == {"active": True, "mic": False}
                for s in snapshots[:2])
        and snapshots[2]["sqlite"]["status"] == "CANCELLED"
        and snapshots[2]["sqlite"]["end_reason"] == "DISCONNECTED"
        and snapshots[2]["voice"] == {"active": False, "mic": False}
        and len([r for r in responses if r["method"] == "GET"]) >= 4
        and all(r["status"] == 200 for r in responses)
    )
    if known_failure:
        raise MutedInvitationLeaseExpired("Active muted foreground call made no lease renewals and was stopped as DISCONNECTED after 90 seconds")
    assert all(s["sqlite"]["status"] == "ACCEPTED" and s["voice"] == {"active": True, "mic": not muted}
               for s in snapshots)
    assert len(renewal) == 3 and all(r["token_present"] and r["status"] == 200 for r in renewal)
