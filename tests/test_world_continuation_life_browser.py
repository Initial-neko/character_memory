import os
from datetime import datetime, timezone
from pathlib import Path
import pytest
from test_life_conversation import isolated_app
from test_life_conversation_browser import live_life
from character_memory.world_activity import WorldPulseRepository

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser run')]


@pytest.mark.parametrize('width',[390,1440])
def test_continuation_receipts_render_readonly_in_existing_life_detail(page,live_life,width):
    from playwright.sync_api import expect
    base,character,event,store,access=live_life
    repo=WorldPulseRepository(store)
    now=datetime.now(timezone.utc)
    step={'character_id':character,'opportunity_id':'ui-read','request_id':'ui-read:CONT:1','capability':'BROWSER_OPEN','status':'SUCCESS','data':{'observation':{'final_url':'https://example.com/actually-read','text':'Actual rendered page text'}}}
    for name,phase,result in [
        ('ui-stop','APPLIED',{'continuation':True,'steps':[],'execution_status':'SUCCESS','stop_reason':'MODEL_STOP'}),
        ('ui-read','APPLIED',{'continuation':True,'steps':[step],'execution_status':'SUCCESS','stop_reason':'MODEL_STOP','kept':False,'summary':'<script>window.FORGED=true</script>'}),
        ('ui-unknown','FAILED',{'continuation':True,'steps':[],'execution_status':'UNKNOWN','stop_reason':'TimeoutError'})]:
        repo.claim_browse_decision(name,character,now)
        repo.update_browse_decision(name,expected_phase='PLANNING',phase=phase,plan={'mode':'CONTINUATION'},result=result)
    before=store.conn.total_changes
    requests=[]
    page.on('request',lambda r:requests.append((r.method,r.url)))
    page.set_viewport_size({'width':width,'height':844 if width==390 else 960})
    page.goto(base+'/life?character='+character)
    page.locator('[data-channel="WORLD"]').click()
    expect(page.locator('.event-card')).to_have_count(3)
    page.locator('[data-key="world:ui-read"]').click()
    expect(page.locator('#details')).to_contain_text('ui-read:CONT:1')
    expect(page.locator('#details')).to_contain_text('https://example.com/actually-read')
    expect(page.locator('#details')).to_contain_text('BROWSER_RENDERED_TEXT')
    assert page.evaluate('window.FORGED') is None
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    output=os.getenv('WORLD_LIFE_SCREENSHOTS')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(Path(output)/f'continuation-{width}.png'),full_page=True)
    if width==390:page.locator('[data-life-back]').click()
    page.locator('[data-key="world:ui-stop"]').click()
    expect(page.locator('#details')).to_contain_text('未行动')
    if width==390:page.locator('[data-life-back]').click()
    page.locator('[data-key="world:ui-unknown"]').click()
    expect(page.locator('#details')).to_contain_text('结果未知')
    assert store.conn.total_changes==before
    assert not [r for r in requests if r[0]=='POST']
    page.goto('about:blank')
