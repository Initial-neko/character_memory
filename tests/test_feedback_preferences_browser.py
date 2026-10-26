"""Actual UI/HTTP explicit preference application, using an isolated local provider."""
import json,os
import pytest
from test_reply_suggestions_browser import suggestions_server,start,generate,signals,capture
pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser runtime')]

def open_after_send(page,base,width=1280):
    from playwright.sync_api import expect
    start(page,base,width)
    batch=generate(page)
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    with page.expect_response(lambda r:r.url.endswith('/v1/chat/messages') and r.request.method=='POST'):
        page.locator('#sendButton').click()
    page.get_by_role('button',name='建议反馈',exact=True).click()
    box=page.get_by_role('dialog',name='建议反馈',exact=True)
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    box.get_by_text('同时调整相处偏好（可选）',exact=True).click()
    return batch,box

@pytest.mark.parametrize('width',[1280,390])
def test_explicit_feedback_reaches_next_reply_and_suggestions(page,suggestions_server,tmp_path,width):
    from playwright.sync_api import expect
    batch,box=open_after_send(page,suggestions_server,width)
    box.get_by_label('反馈内容',exact=True).fill('希望以后更简短一点')
    box.get_by_label('反馈回答长度',exact=True).select_option('BRIEF')
    capture(page,tmp_path,f'feedback-preference-{width}-draft')
    with page.expect_response(lambda r:r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method=='POST') as result:
        box.get_by_role('button',name='提交反馈',exact=True).click()
    assert result.value.status==200,result.value.text()
    assert result.value.json()['applied_profile_version']==1
    expect(box.get_by_role('status')).to_contain_text('下次回复')
    assert page.request.get(suggestions_server+'/v2/me/experience-profile').json()['preferences']=={'verbosity':'BRIEF'}
    capture(page,tmp_path,f'feedback-preference-{width}-saved')
    assert page.request.post(suggestions_server+'/v1/chat',data={'character_id':'rin','message':'继续聊聊聚餐'}).status==200
    contexts=[json.loads(x) for x in (tmp_path/'profile-contexts.jsonl').read_text(encoding='utf-8').splitlines()]
    assert contexts[-1]['profile_version']==1 and contexts[-1]['brief']
    box.get_by_role('button',name='关闭建议反馈').click()
    page.evaluate('CM.loadHistory()')
    generate(page)
    calls=[json.loads(x) for x in (tmp_path/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert calls[-1]['profile_version']==1 and calls[-1]['preferences']=={'verbosity':'BRIEF'}
    assert all(x['external_calls']==0 for x in calls)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')

def test_profile_conflict_requires_explicit_rebase(page,suggestions_server,tmp_path):
    from playwright.sync_api import expect
    batch,box=open_after_send(page,suggestions_server,390)
    box.get_by_label('反馈内容',exact=True).fill('保留我的反馈草稿')
    box.get_by_label('反馈回答长度',exact=True).select_option('BRIEF')
    assert page.request.patch(suggestions_server+'/v2/me/experience-profile',data={'expected_version':0,'changes':{'tone':'CUTE'}}).status==200
    box.get_by_role('button',name='提交反馈',exact=True).click()
    expect(box.get_by_role('status')).to_contain_text('未覆盖')
    expect(box.get_by_label('反馈内容',exact=True)).to_have_value('保留我的反馈草稿')
    expect(box.get_by_label('反馈回答长度',exact=True)).to_have_value('BRIEF')
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_disabled()
    capture(page,tmp_path,'feedback-preference-390-conflict')
    page.once('dialog',lambda d:d.accept())
    with page.expect_event('dialog'):
        box.get_by_role('button',name='读取最新偏好',exact=True).click()
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    assert page.request.get(suggestions_server+'/v2/me/experience-profile').json()['preferences']=={'tone':'CUTE'}
    with page.expect_response(lambda r:r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method=='POST') as result:
        box.get_by_role('button',name='提交反馈',exact=True).click()
    assert result.value.status==200 and result.value.json()['applied_profile_version']==2
    assert page.request.get(suggestions_server+'/v2/me/experience-profile').json()['preferences']=={'tone':'CUTE','verbosity':'BRIEF'}

def test_unknown_feedback_retry_after_clear_does_not_reapply(page,suggestions_server):
    from playwright.sync_api import expect
    batch,box=open_after_send(page,suggestions_server)
    box.get_by_label('反馈内容',exact=True).fill('只应用这一次')
    box.get_by_label('反馈回答长度',exact=True).select_option('BRIEF')
    sent=[]
    def lost(route):
        sent.append(route.request.post_data_json)
        response=route.fetch()
        if len(sent)==1:route.abort('failed')
        else:route.fulfill(response=response)
    page.route('**/v2/reply-suggestions/feedback',lost)
    box.get_by_role('button',name='提交反馈',exact=True).click()
    expect(box.get_by_role('button',name='重试原提交',exact=True)).to_be_enabled()
    assert page.request.delete(suggestions_server+'/v2/me/experience-profile',params={'expected_version':1}).status==200
    box.get_by_role('button',name='重试原提交',exact=True).click()
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    assert len(sent)==2 and sent[0]==sent[1]
    profile=page.request.get(suggestions_server+'/v2/me/experience-profile').json()
    assert profile['version']==2 and profile['preferences']=={}
    expect(box.get_by_role('status')).to_contain_text('后续更改')
    assert len([x for x in signals(page,suggestions_server,batch) if x['kind']=='EXPLICIT_FEEDBACK'])==1

def test_null_withdrawal_and_restore_use_original_profile(page,suggestions_server):
    from playwright.sync_api import expect
    start(page,suggestions_server)
    assert page.request.patch(suggestions_server+'/v2/me/experience-profile',data={'expected_version':0,'changes':{'tone':'CUTE','verbosity':'DETAILED'}}).status==200
    batch=generate(page)
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    with page.expect_response(lambda r:r.url.endswith('/v1/chat/messages')):
        page.locator('#sendButton').click()
    page.get_by_role('button',name='建议反馈',exact=True).click()
    box=page.get_by_role('dialog',name='建议反馈',exact=True)
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    box.get_by_text('同时调整相处偏好（可选）',exact=True).click()
    box.get_by_label('反馈回答长度',exact=True).select_option('_CLEAR')
    box.get_by_label('反馈相处节奏',exact=True).select_option('QUIET')
    with page.expect_response(lambda r:r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method=='POST') as result:
        box.get_by_role('button',name='提交反馈',exact=True).click()
    assert result.value.status==200,result.value.text()
    original=result.value.request.post_data_json
    assert original['preference_changes']=={'energy':'QUIET','verbosity':None}
    expect(box.get_by_role('status')).to_contain_text('下次回复')
    assert page.request.get(suggestions_server+'/v2/me/experience-profile').json()['preferences']=={'tone':'CUTE','energy':'QUIET'}
    box.get_by_role('button',name='查看或恢复相处偏好',exact=True).click()
    profile=page.get_by_role('dialog',name='相处偏好',exact=True)
    profile.get_by_role('button',name='版本记录',exact=True).click()
    profile.locator('[data-restore-version="1"]').click()
    profile.get_by_role('button',name='确认恢复',exact=True).click()
    expect(profile.get_by_role('status')).to_contain_text('已保存')
    restored=page.request.get(suggestions_server+'/v2/me/experience-profile').json()
    assert restored['version']==3 and restored['preferences']=={'tone':'CUTE','verbosity':'DETAILED'}
    assert page.request.post(suggestions_server+'/v2/reply-suggestions/feedback',data=original).status==200
    assert page.request.get(suggestions_server+'/v2/me/experience-profile').json()==restored


def test_feedback_application_survives_real_http_restart(page,tmp_path):
    from contextlib import contextmanager
    from playwright.sync_api import expect
    server=contextmanager(suggestions_server.__wrapped__)
    with server(tmp_path) as base:
        batch,box=open_after_send(page,base)
        box.get_by_label('反馈内容',exact=True).fill('下次简短一点')
        box.get_by_label('反馈回答长度',exact=True).select_option('BRIEF')
        with page.expect_response(lambda r:r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method=='POST') as saved:
            box.get_by_role('button',name='提交反馈',exact=True).click()
        assert saved.value.status==200
        original=saved.value.request.post_data_json
        expect(box.get_by_role('status')).to_contain_text('下次回复')
        row=box.locator('[data-signal-id]').first
        row.get_by_role('button',name='更正',exact=True).click()
        box.get_by_label('反馈内容',exact=True).fill('更正文字不撤销明确选择')
        box.get_by_role('button',name='保存更正',exact=True).click()
        expect(row).to_contain_text('更正文字不撤销明确选择')
        page.once('dialog',lambda d:d.accept())
        row.get_by_role('button',name='撤回',exact=True).click()
        expect(row).to_contain_text('已撤回')
        assert page.request.get(base+'/v2/me/experience-profile').json()['version']==1
    with server(tmp_path) as base:
        profile=page.request.get(base+'/v2/me/experience-profile').json()
        assert profile['version']==1 and profile['preferences']=={'verbosity':'BRIEF'}
        replay=page.request.post(base+'/v2/reply-suggestions/feedback',data=original)
        assert replay.status==200 and replay.json()['withdrawn']
        assert page.request.get(base+'/v2/me/experience-profile').json()==profile
        assert page.request.get(base+'/v2/reply-suggestions/'+batch['batch_id']).status==409
        assert page.request.post(base+'/v1/chat',data={'character_id':'rin','message':'重启后的私聊'}).status==200
        group=page.request.post(base+'/v1/groups',data={'name':'偏好群聊','member_ids':['rin','momo']})
        assert group.status==200,group.text()
        gid=group.json()['group']['id']
        response=page.request.post(base+'/v1/groups/'+gid+'/chat',data={'message':'重启后的群聊'})
        assert response.status==200,response.text()
        entries=[json.loads(x) for x in (tmp_path/'profile-contexts.jsonl').read_text(encoding='utf-8').splitlines()]
        assert any(x['group'] and x['brief'] and x['profile_version']==1 for x in entries)
        assert any(not x['group'] and x['brief'] and x['profile_version']==1 for x in entries)
        assert len({x['pid'] for x in entries})>=2
        assert all(x['external_calls']==0 for x in entries)
    evidence=os.getenv('REPLY_SUGGESTION_UI_EVIDENCE_DIR')
    if evidence:
        import subprocess
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        result={'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
                'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()),
                'profile':profile,'post_restart_replay':replay.json(),'provider_contexts':entries,
                'owned_ports_closed':True,'external_model_calls':0}
        dest=Path(evidence);dest.mkdir(parents=True,exist_ok=True)
        (dest/'feedback-http-restart.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
