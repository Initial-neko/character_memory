"""One actual UI story across a real server restart; fixed provider is not semantic QA."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

import pytest
if os.getenv('RUN_PLAYWRIGHT') == '1':
    from playwright.sync_api import expect
else:
    expect = None
from test_p0_17b_playwright import _open
from test_reply_suggestions_browser import suggestions_server, generate, signals

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser runtime')]


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def send_and_wait(page, base, text):
    page.locator('#messageInput').fill(text)
    with page.expect_response(lambda r: r.url.endswith('/v1/chat/messages') and r.request.method == 'POST') as accepted:
        page.locator('#sendButton').click()
    assert accepted.value.status == 202, accepted.value.text()
    receipt = accepted.value.json()
    expect(page.locator('.message-row.assistant .bubble', has_text='E2E reply: ' + text)).to_have_count(1, timeout=20000)
    expect(page.locator('.message-row.user .bubble', has_text=text)).to_have_count(1)
    source = receipt['event_id']
    trace = page.request.get(base + '/v1/traces/' + str(source))
    assert trace.status == 200, trace.text()
    history = page.request.get(base + '/v1/chat/history', params={'character_id': 'rin'}).json()['messages']
    user = next(row for row in history if row['id'] == source)
    replies = [row for row in history if row.get('role') == 'assistant' and row.get('source_event_id') == source]
    assert user['content'] == text and user.get('has_trace') is True
    assert len(replies) == 1 and replies[0]['content'] == 'E2E reply: ' + text
    return {'receipt': receipt, 'source': user, 'reply': replies[0], 'trace': trace.json()}


def profile_dialog(page):
    page.get_by_role('button', name='相处偏好', exact=True).click()
    box = page.get_by_role('dialog', name='相处偏好', exact=True)
    expect(box.get_by_role('button', name=re.compile('^保存(选择|修改)$'))).to_be_visible()
    return box


@pytest.mark.parametrize('width,height', [(1280, 900), (390, 844)])
def test_m2_full_ui_story_survives_refresh_and_actual_restart(page, tmp_path, monkeypatch, width, height):
    monkeypatch.setenv('M2_FULL_LOOP_STORY', '1')
    server = contextmanager(suggestions_server.__wrapped__)
    evidence = Path(os.getenv('M2_FULL_LOOP_EVIDENCE_DIR', str(tmp_path / 'story-evidence')))
    if os.getenv('M2_PREVIEW_MODE') == '1':
        evidence = evidence / 'isolated-preview'
    evidence.mkdir(parents=True, exist_ok=True)
    pngs, assets, runtimes, steps = [], {}, [], []
    def capture(label, base):
        picture = evidence / f'm2-{width}-{label}.png'
        page.screenshot(path=str(picture), animations='disabled')
        pngs.append({'state': label, 'path': str(picture.resolve()), 'sha256': hashlib.sha256(picture.read_bytes()).hexdigest()})
        for asset in ('experience_profile.js', 'experience_profile.css', 'reply_suggestions.js', 'reply_suggestions.css', 'app.js', 'client_sends.js'):
            response = page.request.get(base + '/static/' + asset)
            assert response.status == 200
            digest = hashlib.sha256(response.body()).hexdigest()
            if asset in assets:
                assert assets[asset] == digest, 'served assets changed across the same story'
            assets[asset] = digest
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.set_viewport_size({'width': width, 'height': height})
    with server(tmp_path) as base:
        _open(page, base)
        runtime = page.request.get(base + '/__e2e/suggestions-runtime').json()
        assert runtime['story'] is True
        runtimes.append(runtime)
        if os.getenv('M2_PREVIEW_MODE') == '1':
            assert page.request.get(base + '/v1/settings').status == 403
            assert page.request.post(base + '/v1/chat/messages', headers={'Origin': 'https://foreign.invalid'}, data={}).status == 403
            assert page.request.post(base + '/v1/groups', data={}).status == 403
        box = profile_dialog(page)
        box.get_by_label('可爱', exact=True).check()
        box.get_by_label('安静', exact=True).check()
        box.get_by_label('丰富', exact=True).check()
        box.get_by_role('button', name='保存选择', exact=True).click()
        expect(box.get_by_role('status')).to_contain_text('已保存')
        initial = page.request.get(base + '/v2/me/experience-profile').json()
        assert initial['version'] == 1 and initial['preferences'] == {'tone': 'CUTE', 'energy': 'QUIET', 'verbosity': 'DETAILED'}
        capture('profile-saved', base)
        box.get_by_role('button', name='关闭相处偏好').click()
        steps.append(send_and_wait(page, base, '完整相处流程的第一句话'))
        batch = generate(page)
        assert batch['profile_version'] == 1
        page.locator('[data-suggestion-id]').first.click()
        expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
        edited = batch['suggestions'][0]['text'] + ' 我想先说说自己的感受。'
        steps.append(send_and_wait(page, base, edited))
        accepted = signals(page, base, batch)
        assert len(accepted) == 1 and accepted[0]['kind'] == 'EDIT'
        assert accepted[0]['source_ref']['id'] == steps[-1]['receipt']['event_id']
        capture('edited-and-answered', base)
        page.get_by_role('button', name='建议反馈', exact=True).click()
        feedback = page.get_by_role('dialog', name='建议反馈', exact=True)
        expect(feedback.get_by_role('button', name='提交反馈', exact=True)).to_be_enabled()
        feedback.get_by_label('反馈内容', exact=True).fill('以后希望先简短说重点')
        feedback.get_by_text('同时调整相处偏好（可选）', exact=True).click()
        feedback.get_by_label('反馈回答长度', exact=True).select_option('BRIEF')
        with page.expect_response(lambda r: r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method == 'POST') as saved:
            feedback.get_by_role('button', name='提交反馈', exact=True).click()
        assert saved.value.status == 200, saved.value.text()
        applied = saved.value.json()
        assert applied['profile_version'] == 1 and applied['applied_profile_version'] == 2
        expect(feedback.get_by_role('status')).to_contain_text('下次回复')
        capture('feedback-applied', base)
        authored = next(row for row in signals(page, base, batch) if row['kind'] == 'EXPLICIT_FEEDBACK')
        feedback_row = feedback.locator('[data-signal-id=' + json.dumps(authored['id']) + ']')
        feedback_row.get_by_role('button', name='更正', exact=True).click()
        feedback.get_by_label('反馈内容', exact=True).fill('希望简短，具体细节需要时再展开')
        with page.expect_response(lambda r: r.request.method == 'PATCH' and r.url.endswith('/feedback/' + authored['id'])) as corrected:
            feedback.get_by_role('button', name='保存更正', exact=True).click()
        assert corrected.value.status == 200, corrected.value.text()
        expect(feedback_row).to_contain_text('希望简短，具体细节需要时再展开')
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url.endswith('/feedback/' + authored['id'] + '/withdraw')) as withdrawn:
            feedback_row.get_by_role('button', name='撤回', exact=True).click()
        assert withdrawn.value.status == 200, withdrawn.value.text()
        expect(feedback_row).to_contain_text('已撤回')
        assert page.request.get(base + '/v2/me/experience-profile').json()['version'] == 2
        capture('feedback-withdrawn-preference-retained', base)
        feedback.get_by_role('button', name='关闭建议反馈').click()
        steps.append(send_and_wait(page, base, '明确反馈之后继续聊'))
        changed = generate(page)
        assert changed['profile_version'] == 2
        calls = read_lines(tmp_path / 'calls.jsonl')
        assert calls[-1]['preferences'] == {'tone': 'CUTE', 'energy': 'QUIET', 'verbosity': 'BRIEF'}
        contexts = read_lines(tmp_path / 'profile-contexts.jsonl')
        assert [row['profile_version'] for row in contexts] == [1, 1, 2]
        assert [row['brief'] for row in contexts] == [False, False, True]
        page.reload()
        page.wait_for_function('() => Boolean(CM.state.directStream && CM.state.directStream.readyState === EventSource.OPEN)')
        expect(page.locator('.message-row.assistant .bubble', has_text='E2E reply: 明确反馈之后继续聊')).to_have_count(1)
        box = profile_dialog(page)
        expect(box.get_by_label('简短', exact=True)).to_be_checked()
        expect(box.get_by_label('可爱', exact=True)).to_be_checked()
        capture('refreshed', base)
        box.get_by_role('button', name='关闭相处偏好').click()
    with server(tmp_path) as base:
        _open(page, base)
        second = page.request.get(base + '/__e2e/suggestions-runtime').json()
        assert second['pid'] != runtime['pid'] and second['commit'] == runtime['commit']
        assert second['source_sha256'] == runtime['source_sha256']
        runtimes.append(second)
        box = profile_dialog(page)
        expect(box.get_by_label('简短', exact=True)).to_be_checked()
        expect(box.get_by_label('安静', exact=True)).to_be_checked()
        box.get_by_role('button', name='关闭相处偏好').click()
        steps.append(send_and_wait(page, base, '重启之后继续这段相处'))
        contexts = read_lines(tmp_path / 'profile-contexts.jsonl')
        assert [row['profile_version'] for row in contexts] == [1, 1, 2, 2]
        assert contexts[-1]['brief'] and contexts[-1]['pid'] == second['pid']
        final = page.request.get(base + '/v2/me/experience-profile').json()
        assert final['version'] == 2 and final['preferences'] == calls[-1]['preferences']
        capture('restarted-and-answered', base)
        if os.getenv('M2_PREVIEW_MODE') == '1':
            page.goto(base + '/preview')
            expect(page.get_by_text('隔离体验 · 固定本地回复', exact=True)).to_be_visible()
            frame = page.frame_locator('iframe')
            expect(frame.locator('#sendButton')).to_be_visible()
            assert frame.locator('html').evaluate('(node) => node.scrollWidth <= innerWidth')
            capture('isolated-preview', base)
        final_signals = signals(page, base, batch)
        assert len([row for row in final_signals if row['kind'] == 'EDIT']) == 1
        assert len([row for row in final_signals if row['kind'] == 'EXPLICIT_FEEDBACK']) == 1
        governed = next(row for row in final_signals if row['kind'] == 'EXPLICIT_FEEDBACK')
        assert governed['withdrawn'] and governed['revision'] == 3 and not governed['comment']
    assert all(row['external_calls'] == 0 for row in calls + contexts)
    record = {'captured_at': datetime.now(timezone.utc).isoformat(), 'viewport': {'width': width, 'height': height},
        'runtimes': runtimes, 'assets_sha256': assets, 'initial_profile': initial, 'final_profile': final,
        'batch': batch, 'subsequent_batch': changed, 'edit_signal': accepted[0], 'feedback': applied,
        'steps': steps, 'suggestion_calls': calls, 'profile_contexts': contexts, 'signals_after_restart': final_signals,
        'pngs': pngs, 'external_model_calls': 0, 'owned_ports_closed': True,
        'provider': 'fixed local deterministic; wiring and persistence only, no semantic quality claim'}
    (evidence / f'm2-{width}-full-loop.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')

