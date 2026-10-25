"""Real browser/HTTP checks for durable client send identity (no paid models)."""
import os
from pathlib import Path
from uuid import uuid4
import pytest
from test_p0_17b_playwright import realtime_server, _open

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser runtime')]

@pytest.fixture
def page(browser):
    # Share pytest-playwright's session runtime; nesting sync_playwright fails
    # when other browser modules have already started that runtime.
    context = browser.new_context(viewport={'width':1280,'height':900})
    yield context.new_page()
    context.close()


def _capture(page, tmp_path, name):
    root=Path(os.getenv('CLIENT_SEND_EVIDENCE_DIR',str(tmp_path)))
    root.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(root/f'{name}.png'),full_page=False,animations='disabled')


def test_intentional_new_identical_sends_have_distinct_keys(page, realtime_server):
    _open(page, realtime_server)
    sent=[]
    page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/v1/chat/messages') else None)
    page.evaluate("async () => {await CM.sendDirectPayload({message:'intentional duplicate'}); await CM.sendDirectPayload({message:'intentional duplicate'});}")
    assert len(sent)==2
    assert sent[0]['request_id'] != sent[1]['request_id']


@pytest.mark.parametrize('width', [1280,390])
def test_lost_ack_refresh_then_explicit_retry_reuses_original_identity(page, realtime_server, width, tmp_path):
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':900 if width==1280 else 844})
    _open(page, realtime_server)
    sent=[]; receipts=[]
    def intercept(route):
        sent.append(route.request.post_data_json)
        result=route.fetch(); receipts.append(result.json())
        if len(sent)==1: route.abort('failed')
        else:
            _capture(page,tmp_path,f'direct-{width}-retrying')
            route.fulfill(response=result)
    page.route('**/v1/chat/messages',intercept)
    marker='ack-lost-'+uuid4().hex[:8]
    page.locator('#messageInput').fill(marker);page.locator('#sendButton').click()
    card=page.locator('.client-send-record[data-state="unknown"]')
    expect(card.locator("..")).to_contain_text(marker)
    page.reload();page.wait_for_function('() => CM.state.directStream?.readyState === EventSource.OPEN')
    expect(card.locator("..")).to_contain_text(marker)
    assert len(sent)==1, 'refresh must not automatically send'
    expect(page.locator('[data-client-pending]')).to_have_count(0)
    _capture(page,tmp_path,f'direct-{width}-unknown')
    card.get_by_role('button',name='重试原发送',exact=True).click()
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert sent[0]==sent[1]
    assert receipts[0]['event_id']==receipts[1]['event_id']
    page.evaluate('CM.loadDirectHistory()')
    count=page.evaluate("m => CM.state.directHistory.messages.filter(x=>x.role==='user' && x.content===m).length",marker)
    assert count==1
    _capture(page,tmp_path,f'direct-{width}-accepted')


def test_storage_failure_prevents_any_http_send(page,realtime_server):
    _open(page,realtime_server)
    sent=[]
    page.on('request',lambda r: sent.append(r.url) if r.url.endswith('/v1/chat/messages') else None)
    page.evaluate("() => {CM.clientSends.storage.put=async()=>{throw new Error('quota full')};}")
    error=page.evaluate("async () => {try{await CM.sendDirectPayload({message:'must not send'});return ''}catch(e){return e.message}}")
    assert error and not sent


@pytest.mark.parametrize('width',[1280,390])
def test_group_image_lost_ack_keeps_final_payload_mentions_and_media(page,realtime_server,width,tmp_path):
    from playwright.sync_api import expect
    import base64
    from io import BytesIO
    from PIL import Image
    page.set_viewport_size({'width':width,'height':900 if width==1280 else 844})
    _open(page,realtime_server)
    target=page.evaluate("""async()=>{const p=(await CM.api('/v1/characters')).characters.slice(0,2);const g=await CM.api('/v1/groups',{method:'POST',body:JSON.stringify({name:'Client retry group',member_ids:p.map(x=>x.id)})});await CM.features.groups.loadGroups();await CM.features.groups.enter(g.group.id);return {id:g.group.id,name:p[0].name};}""")
    sent=[];receipts=[]
    def intercept(route):
        sent.append(route.request.post_data_json);result=route.fetch();receipts.append(result.json())
        if len(sent)==1:route.abort('failed')
        else:
            _capture(page,tmp_path,f'group-{width}-retrying')
            route.fulfill(response=result)
    page.route('**/v1/groups/*/messages',intercept)
    image=BytesIO();Image.new('RGB',(3,3),'red').save(image,format='PNG')
    data='data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()
    page.evaluate("""async x=>{try{await CM.features.groups.sendImage({filename:'retry.png',data_url:x.data},'@'+x.name+' 图片重试')}catch(e){}}""",{**target,'data':data})
    card=page.locator('.client-send-record[data-state="unknown"]');expect(card).to_be_visible()
    assert sent[0]['mentions'], 'the persisted payload must include mention enrichment'
    page.reload();page.wait_for_function('()=>CM.state.directStream?.readyState === EventSource.OPEN')
    if width==390:page.locator('#sidebarMobileButton').click()
    page.locator('[data-group="'+target['id']+'"]').click()
    expect(card).to_be_visible();assert len(sent)==1
    expect(page.locator('.message-row.user[data-client-request-id="'+sent[0]['request_id']+'"]')).to_be_visible()
    expect(page.locator('[data-client-pending]')).to_have_count(0)
    _capture(page,tmp_path,f'group-{width}-unknown')
    card.get_by_role('button',name='重试原发送',exact=True).click()
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert sent[0]==sent[1]
    assert receipts[0]['event_id']==receipts[1]['event_id']
    assert receipts[0]['message']['media_id']==receipts[1]['message']['media_id']
    expect(page.locator('[data-client-pending]')).to_have_count(0)
    _capture(page,tmp_path,f'group-{width}-accepted')


def test_rejection_is_retained_and_editing_creates_a_new_send(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    sent=[]
    def intercept(route):
        sent.append(route.request.post_data_json)
        if len(sent)==1:route.fulfill(status=422,content_type='application/json',body='{"detail":"fixture rejection"}')
        else:route.continue_()
    page.route('**/v1/chat/messages',intercept)
    page.locator('#messageInput').fill('keep rejected draft');page.locator('#sendButton').click()
    card=page.locator('.client-send-record[data-state="rejected"]');expect(card).to_be_visible()
    card.get_by_role('button',name='编辑为新消息',exact=True).click()
    expect(page.locator('#messageInput')).to_have_value('keep rejected draft')
    page.locator('#messageInput').fill('edited draft');page.locator('#sendButton').click()
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert sent[0]['request_id']!=sent[1]['request_id']
    assert sent[1]['message']=='edited draft'


def test_history_eviction_does_not_recreate_acknowledged_messages(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    page.evaluate("CM.sendDirectPayload({message:'accepted projection only'})")
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    page.evaluate("()=>{CM.dom.chat.replaceChildren();}")
    expect(page.locator('[data-client-pending]')).to_have_count(0)


def test_unknown_group_retry_404_preserves_identity_until_restored(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    group=page.evaluate("""async()=>{const p=(await CM.api('/v1/characters')).characters.slice(0,2);const g=(await CM.api('/v1/groups',{method:'POST',body:JSON.stringify({name:'Unknown archived',member_ids:p.map(x=>x.id)})})).group;await CM.features.groups.loadGroups();await CM.features.groups.enter(g.id);return g.id}""")
    sent=[]; receipts=[]
    def intercept(route):
        sent.append(route.request.post_data_json)
        response=route.fetch()
        if response.status==202:receipts.append(response.json())
        if len(sent)==1:route.abort('failed')
        else:route.fulfill(response=response)
    page.route('**/v1/groups/*/messages',intercept)
    page.locator('#messageInput').fill('confirm original after restore');page.locator('#sendButton').click()
    card=page.locator('.client-send-record[data-state="unknown"]');expect(card).to_be_visible()
    page.evaluate("id=>fetch('/v1/groups/'+id+'/archive',{method:'POST'})",group)
    with page.expect_response(lambda response: response.url.endswith('/messages') and response.status==404):
        card.get_by_role('button',name='重试原发送',exact=True).click()
    expect(card).to_be_visible()
    expect(page.locator('.client-send-record[data-state="rejected"]')).to_have_count(0)
    page.evaluate("id=>fetch('/v1/groups/'+id+'/restore',{method:'POST'})",group)
    card.get_by_role('button',name='重试原发送',exact=True).click()
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert len(sent)==3 and sent[0]==sent[1]==sent[2]
    assert receipts[0]['event_id']==receipts[1]['event_id']


def test_rejected_image_editor_shows_original_attachment(page,realtime_server):
    from playwright.sync_api import expect
    import base64
    from io import BytesIO
    from PIL import Image
    _open(page,realtime_server)
    page.route('**/v1/chat/messages',lambda route:route.fulfill(status=422,content_type='application/json',body='{"detail":"fixture rejection"}'))
    image=BytesIO();Image.new('RGB',(2,2),'blue').save(image,format='PNG')
    data='data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()
    page.evaluate("async data=>{try{await CM.sendDirectPayload({message:'edit image',image:{filename:'original.png',data_url:data}})}catch(e){}}",data)
    page.locator('.client-send-record[data-state="rejected"]').get_by_role('button',name='编辑为新消息',exact=True).click()
    expect(page.locator('.image-draft-preview img')).to_be_visible()
    expect(page.locator('#imageCaption')).to_have_value('edit image')


def test_concurrent_retries_share_one_transport_and_scope_isolation(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    original=page.evaluate('CM.state.characterId')
    sent=[]
    def intercept(route):
        sent.append(route.request.post_data_json);response=route.fetch()
        if len(sent)==1:route.abort('failed')
        else:route.fulfill(response=response)
    page.route('**/v1/chat/messages',intercept)
    page.locator('#messageInput').fill('scope retry once');page.locator('#sendButton').click()
    card=page.locator('.client-send-record[data-state="unknown"]');expect(card).to_be_visible()
    key=card.get_attribute('data-send-id')
    other=page.evaluate('CM.state.characters.find(x=>x.id!==CM.state.characterId).id')
    page.locator('[data-character="'+other+'"]').click()
    expect(card).to_have_count(0)
    assert page.evaluate("async id=>{try{await CM.clientSends.retry(id);return false}catch{return true}}",key)
    assert len(sent)==1
    page.locator('[data-character="'+original+'"]').click();expect(card).to_be_visible()
    page.evaluate('id=>Promise.all([CM.clientSends.retry(id),CM.clientSends.retry(id)])',key)
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert len(sent)==2 and sent[0]==sent[1]


def test_life_reference_lost_ack_retry_preserves_reference_and_source(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    source=page.evaluate("async()=>{const r=await CM.sendDirectPayload({message:'source experience for client retry'});return {character:CM.state.characterId,id:r.event_id}}")
    page.evaluate("async x=>{const u=new URL(location.href);u.searchParams.set('life_character',x.character);u.searchParams.set('life_key','events:'+x.id);history.replaceState(null,'',u);await CM.openLifeReference()}",source)
    sent=[];receipts=[]
    def intercept(route):
        sent.append(route.request.post_data_json);response=route.fetch();receipts.append(response.json())
        if len(sent)==1:route.abort('failed')
        else:route.fulfill(response=response)
    page.route('**/v1/chat/messages',intercept)
    page.locator('#messageInput').fill('continue this real source');page.locator('#sendButton').click()
    card=page.locator('.client-send-record[data-state="unknown"]');expect(card).to_be_visible()
    assert sent[0]['life_reference']=={'character_id':source['character'],'key':'events:'+str(source['id'])}
    card.get_by_role('button',name='重试原发送',exact=True).click()
    expect(page.locator('.client-send-record[data-state="unknown"]')).to_have_count(0)
    assert sent[0]==sent[1] and receipts[0]['event_id']==receipts[1]['event_id']


def test_ack_cache_is_bounded_without_deleting_unknown_or_rejected_records(page,realtime_server):
    _open(page,realtime_server)
    result=page.evaluate("""async()=>{
      const records=[];
      for(let i=0;i<105;i++)records.push({id:'cache-'+i,state:'accepted',createdAt:i,ackAt:i});
      records.push({id:'keep-unknown',state:'unknown'},{id:'keep-rejected',state:'rejected'});
      for(const record of records)await CM.clientSends.storage.put(record);
      const saved=await CM.clientSends.storage.list();
      return {accepted:saved.filter(x=>x.state==='accepted').length,unknown:saved.some(x=>x.id==='keep-unknown'),rejected:saved.some(x=>x.id==='keep-rejected')};
    }""")
    assert result=={'accepted':100,'unknown':True,'rejected':True}


def test_indexeddb_upgrade_keeps_existing_unknown_send(page,realtime_server):
    page.goto(realtime_server+'/v1/characters')
    page.evaluate("""async()=>{
      const name='character-memory-client-sends';
      await new Promise((resolve,reject)=>{const r=indexedDB.deleteDatabase(name);r.onsuccess=resolve;r.onerror=reject;r.onblocked=reject});
      await new Promise((resolve,reject)=>{
        const r=indexedDB.open(name,1);
        r.onupgradeneeded=()=>r.result.createObjectStore('sends',{keyPath:'id'});
        r.onsuccess=()=>{const db=r.result,tx=db.transaction('sends','readwrite');tx.objectStore('sends').put({id:'old-pending',state:'unknown',body:'original'});tx.oncomplete=()=>{db.close();resolve()}};
        r.onerror=reject;
      });
    }""")
    page.goto(realtime_server);page.wait_for_function('()=>Boolean(CM.clientSends)')
    saved=page.evaluate("async()=>CM.clientSends.storage.get('old-pending')")
    assert saved=={'id':'old-pending','state':'unknown','body':'original'}


@pytest.mark.parametrize('width',[1280,390])
@pytest.mark.parametrize('scope',['direct','group'])
def test_real_api_rejection_visible_and_editable(page,realtime_server,width,scope,tmp_path):
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':900 if width==1280 else 844})
    _open(page,realtime_server)
    if scope=='group':
        page.evaluate("""async()=>{const ids=CM.state.characters.slice(0,2).map(x=>x.id);const g=await CM.api('/v1/groups',{method:'POST',body:JSON.stringify({name:'Rejected send',member_ids:ids})});await CM.features.groups.loadGroups();await CM.features.groups.enter(g.group.id)}""")
    if scope=='direct':page.evaluate("async()=>{await CM.sendDirectPayload({message:'existing conversation '.repeat(100)});CM.scrollToBottom(false)}")
    with page.expect_response(lambda r:'/messages' in r.url and r.request.method=='POST') as reply:
        page.evaluate("""async scope=>{try{const path=scope==='group'?'/v1/groups/'+encodeURIComponent(CM.state.conversation.groupId)+'/messages':'/v1/chat/messages';const payload={message:'保留这条未发送的消息',sticker_id:'nonexistent-client-acceptance-sticker',...(scope==='direct'?{character_id:CM.state.characterId,conversation_id:CM.conversationIdFor(CM.state.characterId)}:{})};await CM.api(path,{method:'POST',body:JSON.stringify(payload)})}catch{}}""",scope)
    assert 400<=reply.value.status<500
    card=page.locator('.client-send-record[data-state="rejected"]');expect(card).to_be_visible()
    assert card.bounding_box()['y']+card.bounding_box()['height'] <= page.locator('.composer-wrap').bounding_box()['y'], 'new rejection card must stay above the fixed composer'
    _capture(page,tmp_path,f'{scope}-{width}-rejected')
    card.get_by_role('button',name='编辑为新消息',exact=True).click()
    expect(page.locator('#messageInput')).to_have_value('保留这条未发送的消息')
