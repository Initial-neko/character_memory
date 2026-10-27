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
    box.locator(f'[data-compare-version="{version}"]').click()
    expect(box.locator('.cm-experience-compare-table')).to_be_visible()
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


TARGET={'tone':'NATURAL','energy':'QUIET','verbosity':'DETAILED'}
CURRENT={'tone':'CUTE','verbosity':'BRIEF'}


def seed_profile(page,base,steps):
    current=page.request.get(base+'/v2/me/experience-profile').json()
    deleted=page.request.delete(base+'/v2/me/experience-profile',params={'expected_version':current['version']})
    assert deleted.ok,deleted.text()
    version=page.request.get(base+'/v2/me/experience-profile').json()['version']
    saved=[]
    for changes in steps:
        result=page.request.patch(base+'/v2/me/experience-profile',data={'expected_version':version,'changes':changes})
        assert result.ok,result.text()
        saved.append(result.json());version=saved[-1]['version']
    return saved


def seed_two_changes(page,base):
    return seed_profile(page,base,[TARGET,CURRENT])


def open_profile(page,base,width):
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':900 if width==1280 else 844})
    _open(page,base)
    page.get_by_role('button',name='相处偏好',exact=True).click()
    box=page.get_by_role('dialog',name='相处偏好',exact=True)
    expect(box.get_by_role('button',name=re.compile('^保存(选择|修改)$'))).to_be_visible()
    return box


def open_history(box):
    from playwright.sync_api import expect
    box.get_by_role('button',name='版本记录',exact=True).click()
    expect(box.locator('[data-profile-history] .cm-experience-version').first).to_be_visible()


def compare_version(box,version):
    from playwright.sync_api import expect
    box.locator(f'[data-compare-version="{version}"]').click()
    expect(box.locator('.cm-experience-compare-table')).to_be_visible()


def profile_requests(page):
    seen=[]
    def note(request):
        if '/v2/me/experience-profile' in request.url:seen.append((request.method,request.url))
    page.on('request',note)
    return seen


@pytest.mark.parametrize('width',[1280,390])
def test_compare_shows_two_changes_one_same_then_restores_new_version(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    seen=profile_requests(page)
    posts=[]
    def note_post(request):
        if request.method=='POST' and request.url.endswith('/restore'):posts.append(request.post_data_json)
    page.on('request',note_post)
    box=open_profile(page,base,width)
    open_history(box)
    compare_version(box,target['version'])
    # Compare is read-only: opening, history, and both comparison reads are GETs only.
    assert seen and all(method=='GET' for method,_ in seen)
    expect(box.locator('.cm-experience-compare-meta')).to_have_text(
        f'当前读取版本 {current["version"]} · 目标版本 {target["version"]}')
    table=box.locator('.cm-experience-compare-table')
    expected=[
        ('tone','可爱','自然','changed','将改变'),
        ('energy','安静','安静','same','保持不变'),
        ('verbosity','简短','丰富','changed','将改变'),
    ]
    for key,current_value,restored_value,state,label in expected:
        row=table.locator(f'tr[data-compare-key="{key}"]')
        expect(row.locator('[data-current]')).to_have_text(current_value)
        expect(row.locator('[data-restored]')).to_have_text(restored_value)
        expect(row).to_have_attribute('data-change',state)
        expect(row).to_contain_text(label)
    capture(page,tmp_path,f'profile-compare-{width}-compare')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert box.evaluate('(d)=>d.scrollWidth<=d.clientWidth')
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已恢复为新版本')
    assert len(posts)==1
    assert posts[0]['expected_version']==current['version'] and posts[0]['target_version']==target['version']
    restored=page.request.get(base+'/v2/me/experience-profile').json()
    assert restored['version']==current['version']+1
    assert restored['preferences']==target['preferences']
    history=page.request.get(base+'/v2/me/experience-profile/versions').json()['versions']
    assert history[0]['version']==restored['version'] and history[0]['operation']=='RESTORE'
    assert {target['version'],current['version']} <= {item['version'] for item in history}
    # PersonContextBuilder reads this same projection on the next turn; direct/group
    # prompt assertions for the newest version live in tests/test_experience_profile.py.


@pytest.mark.parametrize('width',[1280,390])
def test_compare_same_values_still_creates_a_new_version(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    base=realtime_server
    same={'tone':'NATURAL','energy':'QUIET','verbosity':'BRIEF'}
    target,current=seed_profile(page,base,[same,same])
    box=open_profile(page,base,width)
    open_history(box)
    compare_version(box,target['version'])
    table=box.locator('.cm-experience-compare-table')
    for key in ('tone','energy','verbosity'):
        expect(table.locator(f'tr[data-compare-key="{key}"]')).to_have_attribute('data-change','same')
    expect(box.get_by_text('三项选择都相同；确认仍会保存一个新版本。',exact=True)).to_be_visible()
    capture(page,tmp_path,f'profile-compare-{width}-same')
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已恢复为新版本')
    restored=page.request.get(base+'/v2/me/experience-profile').json()
    assert restored['version']==current['version']+1 and restored['preferences']==target['preferences']
    history=page.request.get(base+'/v2/me/experience-profile/versions').json()['versions']
    assert history[0]['version']==restored['version'] and history[0]['operation']=='RESTORE'


@pytest.mark.parametrize('width',[1280,390])
def test_compare_loading_close_and_late_reply_are_isolated(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    box=open_profile(page,base,width)
    open_history(box)
    held=[]
    state={'hold':True}
    profile_pattern=re.compile(r'/v2/me/experience-profile$')
    versions_pattern=re.compile(r'/v2/me/experience-profile/versions$')
    def hold_profile(route):
        if state['hold']:held.append(('profile',route))
        else:route.continue_()
    def hold_versions(route):
        if state['hold']:held.append(('versions',route))
        else:route.continue_()
    page.route(profile_pattern,hold_profile);page.route(versions_pattern,hold_versions)
    box.locator(f'[data-compare-version="{target["version"]}"]').click()
    expect(box.get_by_role('status')).to_contain_text('正在读取当前选择与版本记录')
    page.wait_for_timeout(100)
    capture(page,tmp_path,f'profile-compare-{width}-loading')
    box.get_by_role('button',name='关闭相处偏好').click()
    expect(box).not_to_be_visible()
    state['hold']=False
    page.get_by_role('button',name='相处偏好',exact=True).click()
    expect(box.get_by_role('button',name=re.compile('^保存(选择|修改)$'))).to_be_visible()
    stale_version=current['version']+10
    for kind,route in held:
        if kind=='profile':route.fulfill(json={'version':stale_version,'preferences':{'tone':'NATURAL'},'updated_at':None})
        else:route.fulfill(json={'versions':[{'version':stale_version,'operation':'PATCH','preferences':{'tone':'NATURAL'},'previous_version':current['version'],'at':'stale'},{'version':target['version'],'operation':'PATCH','preferences':target['preferences'],'previous_version':0,'at':'old'}]})
    page.request.get(base+'/v2/me/experience-profile')
    expect(box.locator('.cm-experience-compare-table')).to_have_count(0)
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    expect(box.locator('.cm-experience-compare-meta')).to_have_count(0)
    page.unroute(profile_pattern,hold_profile);page.unroute(versions_pattern,hold_versions)


def test_conversation_change_closes_in_flight_compare(page,realtime_server):
    from playwright.sync_api import expect
    base=realtime_server
    target,_=seed_two_changes(page,base)
    box=open_profile(page,base,390)
    open_history(box)
    held=[]
    profile_pattern=re.compile(r'/v2/me/experience-profile$')
    versions_pattern=re.compile(r'/v2/me/experience-profile/versions$')
    page.route(profile_pattern,lambda route:held.append(('profile',route)))
    page.route(versions_pattern,lambda route:held.append(('versions',route)))
    box.locator(f'[data-compare-version="{target["version"]}"]').click()
    expect(box.get_by_role('status')).to_contain_text('正在读取当前选择与版本记录')
    page.evaluate("CM.emit('conversationChanged', {type:'DIRECT', characterId: CM.state.characterId})")
    expect(box).not_to_be_visible()
    for kind,route in held:
        route.fulfill(status=503,body='stale')
    page.wait_for_timeout(150)
    expect(box).not_to_be_visible()


def test_switch_compare_target_discards_previous_late_reply(page,realtime_server):
    from playwright.sync_api import expect
    base=realtime_server
    first,second,current=seed_profile(page,base,[
        {'tone':'NATURAL'},
        {'tone':'CUTE','energy':'LIVELY'},
        {'tone':'CUTE','energy':'QUIET','verbosity':'BRIEF'},
    ])
    box=open_profile(page,base,390)
    open_history(box)
    held=[]
    state={'hold':True}
    profile_pattern=re.compile(r'/v2/me/experience-profile$')
    versions_pattern=re.compile(r'/v2/me/experience-profile/versions$')
    def hold_profile(route):
        if state['hold']:held.append(('profile',route))
        else:route.continue_()
    def hold_versions(route):
        if state['hold']:held.append(('versions',route))
        else:route.continue_()
    page.route(profile_pattern,hold_profile);page.route(versions_pattern,hold_versions)
    box.locator(f'[data-compare-version="{first["version"]}"]').click()
    expect(box.get_by_role('status')).to_contain_text('正在读取当前选择与版本记录')
    page.wait_for_timeout(100)
    state['hold']=False
    box.get_by_role('button',name='取消',exact=True).click()
    expect(box.locator('[data-profile-history] .cm-experience-version').first).to_be_visible()
    box.locator(f'[data-compare-version="{second["version"]}"]').click()
    expect(box.locator('.cm-experience-compare-meta')).to_have_text(
        f'当前读取版本 {current["version"]} · 目标版本 {second["version"]}')
    expect(box.locator('tr[data-compare-key="energy"] [data-restored]')).to_have_text('热闹')
    for kind,route in held:
        if kind=='profile':route.fulfill(json={'version':current['version'],'preferences':current['preferences'],'updated_at':None})
        else:route.fulfill(json={'versions':[{'version':current['version'],'operation':'PATCH','preferences':current['preferences'],'previous_version':second['version'],'at':'now'},{'version':second['version'],'operation':'PATCH','preferences':second['preferences'],'previous_version':first['version'],'at':'b'},{'version':first['version'],'operation':'PATCH','preferences':first['preferences'],'previous_version':0,'at':'a'}]})
    page.request.get(base+'/v2/me/experience-profile')
    expect(box.locator('.cm-experience-compare-meta')).to_have_text(
        f'当前读取版本 {current["version"]} · 目标版本 {second["version"]}')
    expect(box.locator('tr[data-compare-key="energy"] [data-restored]')).to_have_text('热闹')
    page.unroute(profile_pattern,hold_profile);page.unroute(versions_pattern,hold_versions)


@pytest.mark.parametrize('width',[1280,390])
def test_compare_rejects_inconsistent_projection_and_read_failure(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    seen=profile_requests(page)
    box=open_profile(page,base,width)
    open_history(box)
    real=page.request.get(base+'/v2/me/experience-profile').json()
    pattern=re.compile(r'/v2/me/experience-profile$')
    def inconsistent(route):route.fulfill(json={**real,'version':real['version']+1})
    page.route(pattern,inconsistent)
    box.locator(f'[data-compare-version="{target["version"]}"]').click()
    expect(box.get_by_role('alert')).to_contain_text('读取不一致')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    expect(box.get_by_role('button',name='返回版本记录',exact=True)).to_be_visible()
    expect(box.get_by_role('button',name='重新比较',exact=True)).to_be_visible()
    capture(page,tmp_path,f'profile-compare-{width}-unavailable')
    page.unroute(pattern,inconsistent)
    def unavailable(route):route.fulfill(status=503,body='unavailable')
    page.route(pattern,unavailable)
    box.get_by_role('button',name='重新比较',exact=True).click()
    expect(box.get_by_role('alert')).to_contain_text('暂时无法比较偏好')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    page.unroute(pattern,unavailable)
    box.get_by_role('button',name='重新比较',exact=True).click()
    expect(box.locator('.cm-experience-compare-table')).to_be_visible()
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_be_enabled()
    assert all(method=='GET' for method,_ in seen)


@pytest.mark.parametrize('width',[1280,390])
def test_compare_removed_target_cannot_be_restored(page,realtime_server,tmp_path,width):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    box=open_profile(page,base,width)
    open_history(box)
    compare_version(box,target['version'])
    cleared=page.request.delete(base+'/v2/me/experience-profile',params={'expected_version':current['version']})
    assert cleared.ok,cleared.text()
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('alert')).to_contain_text('另一处更新')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    expect(box.get_by_role('button',name='重新比较',exact=True)).to_be_visible()
    box.get_by_role('button',name='重新比较',exact=True).click()
    expect(box.get_by_role('alert')).to_contain_text('旧版本已清除')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    capture(page,tmp_path,f'profile-compare-{width}-removed')
    # Explicit re-read is the only recovery; the stale intent never writes.
    history=page.request.get(base+'/v2/me/experience-profile/versions').json()['versions']
    assert history[0]['operation']=='CLEAR'
    assert all(item['version']!=target['version'] for item in history)
    rejected=page.request.post(base+'/v2/me/experience-profile/restore',
        data={'expected_version':history[0]['version'],'target_version':target['version']})
    assert rejected.status==404
    assert page.request.get(base+'/v2/me/experience-profile').json()['version']==history[0]['version']


def test_compare_conflict_409_keeps_intent_and_requires_explicit_recompare(page,realtime_server):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    box=open_profile(page,base,390)
    open_history(box)
    compare_version(box,target['version'])
    posts=[]
    def note(request):
        if request.method=='POST' and request.url.endswith('/restore'):posts.append(request.post_data_json)
    page.on('request',note)
    external=page.request.patch(base+'/v2/me/experience-profile',
        data={'expected_version':current['version'],'changes':{'tone':'NATURAL'}})
    assert external.ok,external.text()
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('alert')).to_contain_text('另一处更新')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    expect(box.get_by_role('button',name='重新比较',exact=True)).to_be_visible()
    assert len(posts)==1
    versions=page.request.get(base+'/v2/me/experience-profile/versions').json()['versions']
    assert versions[0]['operation']=='PATCH' and versions[0]['version']==external.json()['version']
    box.get_by_role('button',name='重新比较',exact=True).click()
    expect(box.locator('.cm-experience-compare-meta')).to_have_text(
        f'当前读取版本 {external.json()["version"]} · 目标版本 {target["version"]}')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_be_enabled()
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('已恢复为新版本')
    assert page.request.get(base+'/v2/me/experience-profile').json()['preferences']==target['preferences']


def test_lost_restore_response_is_unknown_and_not_replayed(page,realtime_server):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    box=open_profile(page,base,390)
    open_history(box)
    compare_version(box,target['version'])
    pattern=re.compile(r'/v2/me/experience-profile/restore$')
    writes=[]
    def lost(route):
        writes.append(route.request.post_data_json)
        response=route.fetch()
        assert response.status==200
        route.abort('failed')
    page.route(pattern,lost)
    box.get_by_role('button',name='确认恢复',exact=True).click()
    expect(box.get_by_role('alert')).to_contain_text('未确认恢复结果')
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_have_count(0)
    assert len(writes)==1
    page.wait_for_timeout(300)
    assert len(writes)==1
    committed=page.request.get(base+'/v2/me/experience-profile').json()
    assert committed['version']==current['version']+1 and committed['preferences']==target['preferences']
    assert page.request.get(base+'/v2/me/experience-profile/versions').json()['versions'][0]['operation']=='RESTORE'
    page.unroute(pattern,lost)
    box.get_by_role('button',name='重新比较',exact=True).click()
    expect(box.locator('.cm-experience-compare-meta')).to_have_text(
        f'当前读取版本 {committed["version"]} · 目标版本 {target["version"]}')
    assert len(writes)==1
    expect(box.get_by_role('button',name='确认恢复',exact=True)).to_be_enabled()


def test_cancel_compare_keeps_unsaved_draft_without_rebasing(page,realtime_server):
    from playwright.sync_api import expect
    base=realtime_server
    target,current=seed_two_changes(page,base)
    seen=profile_requests(page)
    box=open_profile(page,base,390)
    box.get_by_label('可爱',exact=True).check()
    box.get_by_label('丰富',exact=True).check()
    open_history(box)
    compare_version(box,target['version'])
    expect(box.locator('tr[data-compare-key="tone"] [data-restored]')).to_have_text('自然')
    box.get_by_role('button',name='取消',exact=True).click()
    expect(box.locator('[data-profile-history]')).to_be_visible()
    box.get_by_role('button',name='返回偏好',exact=True).click()
    expect(box.get_by_label('可爱',exact=True)).to_be_checked()
    expect(box.get_by_label('安静',exact=True)).to_be_checked()
    expect(box.get_by_label('丰富',exact=True)).to_be_checked()
    expect(box.locator('[data-profile-unsaved]')).to_contain_text('可爱 · 安静 · 丰富')
    assert all(method=='GET' for method,_ in seen)
    assert page.request.get(base+'/v2/me/experience-profile').json()==current
