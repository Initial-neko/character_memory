"""Real Chromium + isolated Profile HTTP; no external model calls."""
import os
import re
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from test_p0_17b_playwright import realtime_server, _open

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser runtime')]


def start(page,base,width=1280):
    page.set_viewport_size({'width':width,'height':900 if width==1280 else 844})
    current=page.request.get(base+'/v2/me/experience-profile').json()
    page.request.delete(base+'/v2/me/experience-profile',params={'expected_version':current['version']})
    _open(page,base)
    page.get_by_role('button',name='相处偏好',exact=True).click()
    from playwright.sync_api import expect
    expect(page.get_by_role('button',name=re.compile('^保存(选择|修改)$'))).to_be_visible()
    return page.get_by_role('dialog',name='相处偏好',exact=True)


def capture(page,tmp_path,name):
    root=Path(os.getenv('EXPERIENCE_UI_EVIDENCE_DIR',str(tmp_path)))
    root.mkdir(parents=True,exist_ok=True)
    picture=root/f'{name}.png'
    page.screenshot(path=str(picture),animations='disabled')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    sources={asset:hashlib.sha256(page.request.get(page.url.rstrip('/')+'/static/'+asset).body()).hexdigest()
             for asset in ['experience_profile.js','experience_profile.css']}
    evidence={'commit':commit,'dirty':bool(subprocess.check_output(['git','status','--porcelain'],text=True).strip()),
              'origin':page.url,'viewport':page.viewport_size,'browser':page.context.browser.version,
              'captured_at':datetime.now(timezone.utc).isoformat(),'assets_sha256':sources,
              'screenshot_sha256':hashlib.sha256(picture.read_bytes()).hexdigest(),
              'external_model_calls':0,'data':'isolated E2E profile database'}
    (root/f'{name}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')


@pytest.mark.parametrize('width',[1280,390])
def test_profile_save_reload_restore_clear(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    box=start(page,realtime_server,width)
    capture(page,tmp_path,f'profile-{width}-initial')
    box.get_by_label('可爱',exact=True).check()
    box.get_by_label('安静',exact=True).check()
    box.get_by_label('简短',exact=True).check()
    box.get_by_role('button',name=re.compile('^保存(选择|修改)$')).click()
    expect(box.get_by_role('status')).to_contain_text('已保存')
    version=page.request.get(realtime_server+'/v2/me/experience-profile').json()['version']
    capture(page,tmp_path,f'profile-{width}-saved')
    box.get_by_role('button',name='关闭相处偏好').click()
    page.reload()
    page.get_by_role('button',name='相处偏好',exact=True).click()
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    box.get_by_label('自然',exact=True).check()
    box.get_by_role('button',name='保存修改',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已保存')
    box.get_by_role('button',name='版本记录',exact=True).click()
    box.locator(f'[data-restore-version="{version}"]').click()
    capture(page,tmp_path,f'profile-{width}-restore')
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    box.get_by_role('button',name='清除偏好',exact=True).click()
    capture(page,tmp_path,f'profile-{width}-clear')
    box.get_by_role('button',name='确认清除',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已清除')
    history=page.request.get(realtime_server+'/v2/me/experience-profile/versions').json()['versions']
    assert len(history)==1 and history[0]['preferences']=={}
    assert page.request.get(realtime_server+'/v2/me/experience-profile').json()['preferences']=={}
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert box.evaluate('(d)=>d.scrollWidth<=d.clientWidth')
    box.get_by_role('button',name='关闭相处偏好').click()
    expect(page.get_by_role('button',name='相处偏好',exact=True)).to_be_focused()


@pytest.mark.parametrize('width',[1280,390])
def test_conflict_keeps_draft_and_requires_explicit_latest(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    box=start(page,realtime_server,width)
    box.get_by_label('可爱',exact=True).check()
    current=page.request.get(realtime_server+'/v2/me/experience-profile').json()
    page.request.patch(realtime_server+'/v2/me/experience-profile',data={'expected_version':current['version'],'changes':{'tone':'NATURAL'}})
    box.get_by_role('button',name=re.compile('^保存(选择|修改)$')).click()
    expect(box.get_by_role('alert')).to_contain_text('另一处更新')
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    expect(box.get_by_role('button',name=re.compile('^保存(选择|修改)$'))).to_be_disabled()
    capture(page,tmp_path,f'profile-{width}-conflict')
    box.get_by_role('button',name='查看最新偏好',exact=True).click()
    expect(box.get_by_label('自然',exact=True)).to_be_checked()
    expect(box.locator('[data-profile-draft]')).to_contain_text('可爱')
    assert page.request.get(realtime_server+'/v2/me/experience-profile').json()['preferences']=={'tone':'NATURAL'}


def test_network_failure_retains_draft_without_auto_retry(page,realtime_server,tmp_path):
    from playwright.sync_api import expect
    box=start(page,realtime_server,390)
    box.get_by_label('丰富',exact=True).check()
    sent=[]
    def fail(route):
        if route.request.method=='PATCH':
            sent.append(route.request.post_data_json);route.abort('failed')
        else:route.continue_()
    page.route('**/v2/me/experience-profile',fail)
    box.get_by_role('button',name=re.compile('^保存(选择|修改)$')).click()
    expect(box.get_by_role('alert')).to_contain_text('未确认保存')
    expect(box.get_by_label('丰富',exact=True)).to_be_checked()
    capture(page,tmp_path,'profile-390-error')
    assert len(sent)==1
    # A lost response is not replayed automatically: reconcile current before another mutation.
    box.get_by_role('button',name='查看最新偏好',exact=True).click()
    expect(box.locator('[data-profile-draft]')).to_contain_text('丰富')
    assert len(sent)==1


def test_loading_error_and_keyboard_dismiss(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    page.route('**/v2/me/experience-profile',lambda route:route.fulfill(status=503,body='unavailable'))
    page.get_by_role('button',name='相处偏好',exact=True).click()
    box=page.get_by_role('dialog',name='相处偏好',exact=True)
    expect(box.get_by_role('alert')).to_contain_text('暂时无法读取')
    assert box.get_by_role('button',name=re.compile('^保存(选择|修改)$')).count()==0
    page.keyboard.press('Escape')
    expect(box).not_to_be_visible()
    expect(page.get_by_role('button',name='相处偏好',exact=True)).to_be_focused()


def test_lost_save_response_reads_committed_state_without_replaying(page,realtime_server):
    from playwright.sync_api import expect
    box=start(page,realtime_server)
    box.get_by_label('可爱',exact=True).check()
    writes=[]
    def lost(route):
        if route.request.method=='PATCH':
            writes.append(route.request.post_data_json)
            response=route.fetch()
            assert response.status==200
            route.abort('failed')
        else:route.continue_()
    page.route('**/v2/me/experience-profile',lost)
    box.get_by_role('button',name=re.compile('^保存(选择|修改)$')).click()
    expect(box.get_by_role('alert')).to_contain_text('未确认保存')
    expect(box.get_by_role('button',name='清除偏好',exact=True)).to_be_disabled()
    assert page.request.get(realtime_server+'/v2/me/experience-profile').json()['preferences']=={'tone':'CUTE'}
    box.get_by_role('button',name='查看最新偏好',exact=True).click()
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    expect(box.locator('[data-profile-draft]')).to_contain_text('可爱')
    assert len(writes)==1
    box.get_by_role('group',name='表达风格').get_by_label('不指定',exact=True).check()
    page.unroute('**/v2/me/experience-profile',lost)
    box.get_by_role('button',name='保存修改',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已保存')
    assert page.request.get(realtime_server+'/v2/me/experience-profile').json()['preferences']=={}


def test_late_read_cannot_replace_reopened_profile(page,realtime_server):
    from playwright.sync_api import expect
    _open(page,realtime_server)
    current=page.request.get(realtime_server+'/v2/me/experience-profile').json()
    result=page.request.patch(realtime_server+'/v2/me/experience-profile',data={'expected_version':current['version'],'changes':{'tone':'NATURAL'}}).json()
    held=[]
    def delayed(route):
        if route.request.method=='GET' and not held:held.append(route)
        else:route.continue_()
    page.route('**/v2/me/experience-profile',delayed)
    entry=page.get_by_role('button',name='相处偏好',exact=True)
    entry.click()
    box=page.get_by_role('dialog',name='相处偏好',exact=True)
    expect(box.get_by_role('status')).to_contain_text('正在读取')
    box.get_by_role('button',name='关闭相处偏好').click()
    entry.click()
    expect(box.get_by_label('自然',exact=True)).to_be_checked()
    held[0].fulfill(json={**result,'preferences':{'tone':'CUTE'}})
    # A second real API response establishes the event-loop barrier after the old reply.
    page.request.get(realtime_server+'/v2/me/experience-profile')
    expect(box.get_by_label('自然',exact=True)).to_be_checked()
    page.keyboard.press('Escape')
    expect(entry).to_be_focused()
