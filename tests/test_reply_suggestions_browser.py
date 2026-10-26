"""Real browser + isolated local HTTP, with deterministic model output."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from test_p0_17b_playwright import _open

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser runtime')]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def suggestions_server(tmp_path):
    for name in ('rin', 'momo'):
        dest = tmp_path / 'personas' / name
        dest.mkdir(parents=True)
        shutil.copyfile(ROOT / 'personas' / name / 'persona.yaml', dest / 'persona.yaml')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONIOENCODING': 'utf-8',
           'CHARACTER_MEMORY_E2E_DB': str(tmp_path / 'browser.db'),
           'CHARACTER_MEMORY_E2E_MEDIA': str(tmp_path / 'media'),
           'CHARACTER_MEMORY_E2E_PERSONA_ROOT': str(tmp_path / 'personas'),
           'SUGGESTION_CALL_LOG': str(tmp_path / 'calls.jsonl')}
    tree = None
    options = {}
    if os.name == 'nt':
        from character_memory.runtime.process_tree import WindowsProcessTree
        tree = WindowsProcessTree()
        options['creationflags'] = 0x00000004 | 0x08000000
    log_path = tmp_path / 'server.log'
    with log_path.open('wb') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'e2e_suggestions_ui_app:app',
            '--app-dir', str(ROOT / 'tests'), '--host', '127.0.0.1', '--port', str(port),
            '--log-level', 'warning'], cwd=ROOT, env=env, stdout=log, stderr=log, **options)
        try:
            if tree:
                tree.attach_and_resume(proc)
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(log_path.read_text(encoding='utf-8', errors='replace'))
                try:
                    with urlopen(base + '/health', timeout=.5) as result:
                        if result.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('isolated suggestions HTTP readiness timeout')
            yield base
        finally:
            if tree:
                tree.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            for _ in range(50):
                with socket.socket() as probe:
                    if probe.connect_ex(('127.0.0.1', port)) != 0:
                        break
                time.sleep(.02)
            else:
                raise AssertionError('owned suggestions API port remains open')


def capture(page, tmp_path, name):
    root = Path(os.getenv('REPLY_SUGGESTION_UI_EVIDENCE_DIR', str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    picture = root / (name + '.png')
    page.screenshot(path=str(picture), animations='disabled')
    origin = '{0.scheme}://{0.netloc}'.format(urlsplit(page.url))
    assets = {name: hashlib.sha256(page.request.get(origin + '/static/' + name).body()).hexdigest()
              for name in ('reply_suggestions.js', 'reply_suggestions.css')}
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).splitlines()
    runtime = page.request.get(origin + '/__e2e/suggestions-runtime').json()
    data = {'captured_at': datetime.now(timezone.utc).isoformat(), 'api_pid': runtime['pid'],
            'assets_sha256': assets, 'git_status_porcelain': status, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'dirty': bool(status),
            'origin': page.url, 'viewport': page.viewport_size, 'browser': page.context.browser.version,
            'external_model_calls': 0, 'screenshot_sha256': hashlib.sha256(picture.read_bytes()).hexdigest()}
    (root / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def start(page, base, width=1280, marker='suggestion source'):
    page.set_viewport_size({'width': width, 'height': 900 if width == 1280 else 844})
    # A synchronous real chat seeds a stable latest visible character fact.
    response = page.request.post(base + '/v1/chat', data={'character_id': 'rin', 'message': marker})
    assert response.status == 200, response.text()
    _open(page, base)
    from playwright.sync_api import expect
    expect(page.get_by_role('button', name='帮我想想', exact=True)).to_be_visible()


def generate(page):
    from playwright.sync_api import expect
    with page.expect_response(lambda r: r.url.endswith('/v2/reply-suggestions') and r.request.method == 'POST') as result:
        page.get_by_role('button', name='帮我想想', exact=True).click()
    assert result.value.status == 200, result.value.text()
    batch = result.value.json()
    expect(page.get_by_label('回复建议', exact=True).locator('[data-suggestion-id]')).to_have_count(3)
    return batch


def signals(page, base, batch):
    response = page.request.get(base + '/v2/reply-suggestions/' + batch['batch_id'] + '/signals')
    assert response.status == 200
    return response.json()['signals']


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('mode', ['SELECT', 'EDIT', 'FREE_INPUT'])
def test_direct_choice_is_only_draft_until_real_send(page, suggestions_server, tmp_path, width, mode):
    from playwright.sync_api import expect
    start(page, suggestions_server, width)
    assert not (tmp_path / 'calls.jsonl').exists(), 'opening chat must not generate suggestions'
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/v1/chat/messages') else None)
    batch = generate(page)
    choice = batch['suggestions'][0]
    if mode != 'FREE_INPUT':
        page.locator('[data-suggestion-id="' + choice['id'] + '"]').click()
        expect(page.locator('#messageInput')).to_have_value(choice['text'])
    assert sent == [] and signals(page, suggestions_server, batch) == []
    assert len((tmp_path / 'calls.jsonl').read_text(encoding='utf-8').splitlines()) == 1, 'choosing a draft must not call the suggestion model again'
    text = choice['text'] if mode == 'SELECT' else '我想用自己的说法聊聊'
    page.locator('#messageInput').fill(text)
    capture(page, tmp_path, f'suggestions-{width}-{mode.lower()}-draft')
    with page.expect_response(lambda r: r.url.endswith('/v1/chat/messages')) as accepted:
        page.locator('#sendButton').click()
    receipt = accepted.value.json()
    assert accepted.value.status == 202, receipt
    recorded = signals(page, suggestions_server, batch)
    assert len(recorded) == 1 and recorded[0]['kind'] == mode
    assert recorded[0]['source_ref'] == {'kind': 'EVENT', 'id': receipt['event_id'], 'conversation_id': None}
    assert sent[0]['reply_suggestion']['batch_id'] == batch['batch_id']
    source = page.request.get(suggestions_server + '/v1/chat/history', params={'character_id': 'rin'}).json()['messages']
    assert any(row['id'] == receipt['event_id'] and row['content'] == text for row in source)


def test_existing_draft_requires_native_replace_confirmation(page, suggestions_server):
    from playwright.sync_api import expect
    start(page, suggestions_server)
    batch = generate(page)
    first = page.locator('[data-suggestion-id="' + batch['suggestions'][0]['id'] + '"]')
    page.locator('#messageInput').fill('已有草稿')
    page.once('dialog', lambda dialog: dialog.dismiss())
    with page.expect_event('dialog'):
        first.click()
    expect(page.locator('#messageInput')).to_have_value('已有草稿')
    page.once('dialog', lambda dialog: dialog.accept())
    with page.expect_event('dialog'):
        first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    assert signals(page, suggestions_server, batch) == []


@pytest.mark.parametrize('status', [409, 502])
def test_generation_failure_never_automatically_regenerates(page, suggestions_server, tmp_path, status):
    from playwright.sync_api import expect
    start(page, suggestions_server, 390)
    requests = []
    def fail(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=status, json={'detail': 'context changed' if status == 409 else 'generation failed; no automatic replay'})
    page.route('**/v2/reply-suggestions', fail)
    page.get_by_role('button', name='帮我想想', exact=True).click()
    expect(page.get_by_label('回复建议', exact=True).get_by_role('status')).to_contain_text('更新' if status == 409 else '没能生成')
    expect(page.locator('[data-suggestion-id]')).to_have_count(0)
    capture(page, tmp_path, f'suggestions-390-error-{status}')
    page.request.get(suggestions_server + '/health')
    assert len(requests) == 1


def test_lost_ack_retry_keeps_attribution_and_request_identity(page, suggestions_server):
    from playwright.sync_api import expect
    start(page, suggestions_server)
    batch = generate(page)
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    sends, receipts = [], []
    def lost(route):
        sends.append(route.request.post_data_json)
        result = route.fetch()
        receipts.append(result.json())
        if len(sends) == 1:
            route.abort('failed')
        else:
            route.fulfill(response=result)
    page.route('**/v1/chat/messages', lost)
    page.locator('#sendButton').click()
    record = page.locator('.client-send-record[data-state="unknown"]')
    expect(record).to_be_visible()
    assert len(sends) == 1
    page.reload()
    page.wait_for_function('() => document.body.dataset.cmReady === "true"')
    expect(record).to_be_visible()
    assert len(sends)==1, 'refresh must not replay an unknown attributed send'
    record.get_by_role('button', name='重试原发送', exact=True).click()
    expect(page.locator('.client-send-record[data-state="accepted"]')).to_be_visible()
    assert len(sends) == 2 and sends[0] == sends[1]
    assert receipts[0]['event_id'] == receipts[1]['event_id']
    assert len(signals(page, suggestions_server, batch)) == 1


def test_group_selection_and_direct_switch_are_isolated(page, suggestions_server):
    from playwright.sync_api import expect
    start(page, suggestions_server)
    direct = generate(page)
    target = page.evaluate("""async()=>{const r=await CM.api('/v1/groups',{method:'POST',body:JSON.stringify({name:'Suggestions group',member_ids:['rin','momo']})});await CM.features.groups.loadGroups();await CM.features.groups.enter(r.group.id);return r.group.id}""")
    expect(page.locator('[data-suggestion-id]')).to_have_count(0)
    response = page.request.post(suggestions_server + '/v1/groups/' + target + '/messages', data={'message': 'group source', 'request_id': str(uuid4())})
    assert response.status == 202
    page.evaluate('CM.features.groups.enter', target)
    batch = generate(page)
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    assert signals(page, suggestions_server, batch) == []
    with page.expect_response(lambda r: r.url.endswith('/v1/groups/' + target + '/messages')) as result:
        page.locator('#sendButton').click()
    receipt = result.value.json()
    row = signals(page, suggestions_server, batch)[0]
    assert row['kind'] == 'SELECT' and row['source_ref']['kind'] == 'CONVERSATION_EVENT'
    assert row['source_ref']['conversation_id'] == target and row['source_ref']['id'] == receipt['event_id']
    assert signals(page, suggestions_server, direct) == []


def test_profile_change_rejects_old_choice_without_send_or_regeneration(page, suggestions_server):
    from playwright.sync_api import expect
    start(page, suggestions_server)
    requests = []
    page.on('request', lambda r: requests.append(r.url) if r.url.endswith('/v2/reply-suggestions') else None)
    batch = generate(page)
    profile = page.request.get(suggestions_server + '/v2/me/experience-profile').json()
    changed = page.request.patch(suggestions_server + '/v2/me/experience-profile', data={'expected_version': profile['version'], 'changes': {'tone': 'CUTE'}})
    assert changed.status == 200
    page.locator('[data-suggestion-id]').first.click()
    # A candidate must be revalidated before entering the composer.
    expect(page.get_by_label('回复建议', exact=True).get_by_role('status')).to_contain_text('已')
    expect(page.locator('#messageInput')).to_have_value('')
    expect(page.locator('[data-suggestion-id]')).to_have_count(0)
    assert len(requests) == 1 and signals(page, suggestions_server, batch) == []


@pytest.mark.parametrize('width', [1280, 390])
def test_explicit_feedback_after_send_can_be_corrected_and_withdrawn(page, suggestions_server, tmp_path, width):
    from playwright.sync_api import expect
    start(page, suggestions_server, width)
    batch = generate(page)
    profile = page.request.get(suggestions_server + '/v2/me/experience-profile').json()
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    with page.expect_response(lambda r: r.url.endswith('/v1/chat/messages')):
        page.locator('#sendButton').click()
    held = []
    page.route('**/v2/reply-suggestions/*/signals', lambda route: held.append(route))
    with page.expect_request(lambda r: r.url.endswith('/signals')):
        page.get_by_role('button', name='建议反馈', exact=True).click()
    dialog = page.get_by_role('dialog', name='建议反馈', exact=True)
    dialog.get_by_role('textbox', name='反馈内容', exact=True).fill('希望更简短一点')
    held[0].continue_()
    page.unroute('**/v2/reply-suggestions/*/signals')
    expect(dialog.get_by_role('button', name='提交反馈', exact=True)).to_be_enabled()
    expect(dialog.get_by_role('textbox', name='反馈内容', exact=True)).to_have_value('希望更简短一点')
    with page.expect_response(lambda r: r.url.endswith('/v2/reply-suggestions/feedback')) as result:
        dialog.get_by_role('button', name='提交反馈', exact=True).click()
    assert result.value.status == 200, result.value.text()
    row = next(r for r in signals(page, suggestions_server, batch) if r['kind'] == 'EXPLICIT_FEEDBACK')
    assert row['comment'] == '希望更简短一点' and row['applied_profile_version'] is None
    assert page.request.get(suggestions_server + '/v2/me/experience-profile').json() == profile
    capture(page, tmp_path, f'suggestions-{width}-feedback')
    feedback_row = dialog.locator('[data-signal-id=' + json.dumps(row['id']) + ']')
    feedback_row.get_by_role('button', name='更正', exact=True).click()
    dialog.get_by_role('textbox', name='反馈内容', exact=True).fill('这个方向有帮助')
    with page.expect_response(lambda r: '/v2/reply-suggestions/feedback/' in r.url and r.request.method == 'PATCH') as corrected:
        dialog.get_by_role('button', name='保存更正', exact=True).click()
    assert corrected.value.status == 200
    row = next(r for r in signals(page, suggestions_server, batch) if r['kind'] == 'EXPLICIT_FEEDBACK')
    assert row['comment'] == '这个方向有帮助' and row['revision'] == 2
    page.once('dialog', lambda dialog: dialog.accept())
    with page.expect_response(lambda r: r.url.endswith('/withdraw')) as withdrawn:
        feedback_row.get_by_role('button', name='撤回', exact=True).click()
    assert withdrawn.value.status == 200
    row = next(r for r in signals(page, suggestions_server, batch) if r['kind'] == 'EXPLICIT_FEEDBACK')
    assert row['withdrawn'] and row['comment'] is None and row['revision'] == 3
    assert page.request.get(suggestions_server + '/v2/me/experience-profile').json() == profile


def test_ignore_records_only_explicit_action(page, suggestions_server):
    from playwright.sync_api import expect
    start(page, suggestions_server)
    batch = generate(page)
    assert signals(page, suggestions_server, batch) == []
    with page.expect_response(lambda r: r.url.endswith('/v2/reply-suggestions/feedback')):
        page.get_by_role('button', name='不用这些', exact=True).click()
    rows = signals(page, suggestions_server, batch)
    assert len(rows) == 1 and rows[0]['kind'] == 'IGNORE' and rows[0]['source_ref'] is None
    expect(page.locator('[data-suggestion-id]')).to_have_count(0)









def test_feedback_reopen_late_ack_retries_same_note_identity(page,suggestions_server):
    from playwright.sync_api import expect
    start(page,suggestions_server)
    batch=generate(page)
    page.locator('[data-suggestion-id]').first.click()
    expect(page.locator('#messageInput')).to_have_value(batch['suggestions'][0]['text'])
    with page.expect_response(lambda r:r.url.endswith('/v1/chat/messages') and r.request.method=='POST'):
        page.locator('#sendButton').click()
    page.get_by_role('button',name='建议反馈',exact=True).click()
    box=page.get_by_role('dialog',name='建议反馈',exact=True)
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    box.get_by_label('反馈内容',exact=True).fill('保留同一次反馈')
    held=[];sent=[]
    def delayed(route):
        sent.append(route.request.post_data_json)
        response=route.fetch()
        if len(sent)==1:held.append((route,response))
        else:route.fulfill(response=response)
    page.route('**/v2/reply-suggestions/feedback',delayed)
    box.get_by_role('button',name='提交反馈',exact=True).click()
    expect(box.get_by_role('button',name='重试原提交',exact=True)).to_be_visible()
    box.get_by_role('button',name='关闭建议反馈').click()
    page.get_by_role('button',name='建议反馈',exact=True).click()
    expect(box.get_by_role('button',name='重试原提交',exact=True)).to_be_enabled()
    with page.expect_response(lambda r:r.url.endswith('/v2/reply-suggestions/feedback') and r.request.method=='POST'):
        held[0][0].fulfill(response=held[0][1])
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    box.get_by_role('button',name='重试原提交',exact=True).click()
    expect(box.get_by_role('button',name='提交反馈',exact=True)).to_be_enabled()
    assert len(sent)==2 and sent[0]==sent[1]
    notes=[x for x in signals(page,suggestions_server,batch) if x['kind']=='EXPLICIT_FEEDBACK']
    assert len(notes)==1 and notes[0]['comment']=='保留同一次反馈'


def test_pending_result_is_checked_with_get_without_new_generation(page,suggestions_server):
    from playwright.sync_api import expect
    start(page,suggestions_server)
    posts=[]
    def pending(route):
        posts.append(route.request.post_data_json)
        response=route.fetch();data=response.json()
        route.fulfill(status=202,json={**data,'status':'PENDING','suggestions':[]})
    page.route('**/v2/reply-suggestions',pending)
    page.get_by_role('button',name='帮我想想',exact=True).click()
    expect(page.get_by_role('button',name='查看生成结果',exact=True)).to_be_enabled()
    page.get_by_role('button',name='查看生成结果',exact=True).click()
    expect(page.locator('[data-suggestion-id]')).to_have_count(3)
    assert len(posts)==1


def test_lost_generation_ack_retries_original_claim_without_extra_model(page,suggestions_server,tmp_path):
    from playwright.sync_api import expect
    start(page,suggestions_server)
    posts=[]
    def lost(route):
        posts.append(route.request.post_data_json)
        response=route.fetch()
        if len(posts)==1:route.abort('failed')
        else:route.fulfill(response=response)
    page.route('**/v2/reply-suggestions',lost)
    page.get_by_role('button',name='帮我想想',exact=True).click()
    expect(page.get_by_role('button',name='重试原生成',exact=True)).to_be_enabled()
    page.get_by_role('button',name='重试原生成',exact=True).click()
    expect(page.locator('[data-suggestion-id]')).to_have_count(3)
    assert len(posts)==2 and posts[0]==posts[1]
    assert len((tmp_path/'calls.jsonl').read_text(encoding='utf-8').splitlines())==1
