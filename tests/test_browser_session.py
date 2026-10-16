from __future__ import annotations

import threading
import pytest

from character_memory import browser_session as bs


def test_session_contract_exists():
    assert hasattr(bs, 'BrowserSession')


def test_internal_scope_requires_exact_path_and_origin():
    scope = bs.BrowserScope(allow_public=False, internal_origins=(
        bs.BrowserInternalOrigin('http://127.0.0.1:8000', ('/readonly',)),
    ))
    assert scope.classify('http://127.0.0.1:8000/readonly') == 'internal'
    for url in ('http://127.0.0.1:8000/api/direct', 'http://127.0.0.1:8000/readonly?role=other',
                'http://127.0.0.1:8001/readonly', 'http://user@127.0.0.1:8000/readonly',
                'file:///readonly', 'http://127.0.0.1:8000/x/../readonly'):
        with pytest.raises(ValueError):
            scope.classify(url)


def test_public_scope_does_not_authorize_private_or_credentials():
    scope = bs.BrowserScope()
    for url in ('http://127.0.0.1/', 'http://[::1]/', 'http://169.254.169.254/', 'https://user:pass@example.com/'):
        with pytest.raises(ValueError):
            scope.classify(url)


def test_limits_fail_closed_and_close_is_idempotent():
    with pytest.raises(ValueError):
        bs.BrowserSessionBudget(max_pages=0)
    session = bs.BrowserSession()
    session.close()
    session.close()
    with pytest.raises(RuntimeError, match='closed'):
        session.inspect()


def test_session_rejects_wrong_thread_before_starting_process():
    session = bs.BrowserSession()
    failures = []
    def run():
        try:
            session.inspect()
        except Exception as exc:
            failures.append(str(exc))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(2)
    session.close()
    assert failures and 'thread' in failures[0]

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import time


class BrowserFixture(BaseHTTPRequestHandler):
    hits = []
    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path in ('/redirect-secret', '/redirect-two'):
            self.send_response(302)
            self.send_header('Location', '/secret' if self.path == '/redirect-secret' else '/two')
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:1/private')
            self.end_headers()
            return
        if self.path == '/slow':
            time.sleep(3)
        status = 404 if self.path == '/missing' else 200
        bodies = {
            '/one': '<title>First page</title><main>First actual page</main><a href="/two">Read second report</a><script>fetch("/secret"); fetch("/mutate", {method:"POST"})</script>',
            '/two': '<title>Second page</title><main id="content">loading</main><script>setTimeout(()=>document.getElementById("content").textContent="Second rendered actual report",30)</script>',
            '/auto': '<main>Initial page</main><script>setTimeout(()=>location.href="/two",10)</script>',
            '/posts': '<main>POST fixture</main><script>for(let i=0;i<5;i++) fetch("/mutate",{method:"POST"})</script>',
            '/links': '<main>Links fixture</main><a style="opacity:0" href="/two">Hidden</a><a href="https://example.com/' + ('x' * 5000) + '">Oversized</a><a href="/two">Visible report</a>',
            '/empty': '',
            '/large': 'x' * 100000,
        }
        payload = bodies.get(self.path, 'fixture').encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self, *_):
        pass


@pytest.fixture
def browser_fixture():
    BrowserFixture.hits = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), BrowserFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    scope = bs.BrowserScope(allow_public=False, internal_origins=(bs.BrowserInternalOrigin(origin, ('/one', '/two', '/redirect', '/empty', '/missing', '/large', '/slow', '/auto', '/posts', '/redirect-secret', '/redirect-two', '/links')),))
    try:
        yield origin, scope
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_pinned_transport_reads_only_authorized_paths(browser_fixture):
    origin, scope = browser_fixture
    status, headers, payload = bs._read_pinned(origin + '/two', scope, 10000, 1)
    assert status == 200 and b'Second rendered actual report' in payload
    with pytest.raises(ValueError, match='scope'):
        bs._read_pinned(origin + '/secret', scope, 10000, 1)
    assert BrowserFixture.hits == ['/two']
    with pytest.raises(ValueError, match='byte budget'):
        bs._read_pinned(origin + '/large', scope, 100, 1)


def test_operation_timeout_cleans_up_worker_without_browser():
    session = bs.BrowserSession(budget=bs.BrowserSessionBudget(operation_timeout_seconds=.001))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        session.inspect()
    assert time.monotonic() - started < 7
    assert session.closed and session._process is None


def test_wall_clock_expiry_closes_idle_session():
    session = bs.BrowserSession(budget=bs.BrowserSessionBudget(max_seconds=.05))
    time.sleep(.15)
    assert session.closed


def test_concurrent_sessions_have_hard_admission_limit():
    sessions = [bs.BrowserSession() for _ in range(2)]
    try:
        with pytest.raises(RuntimeError, match='concurrent'):
            bs.BrowserSession()
    finally:
        for session in sessions:
            session.close()


@pytest.mark.browser
def test_real_chromium_navigation_references_screenshot_and_cleanup(browser_fixture, tmp_path):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    with bs.BrowserSession(scope=scope) as session:
        first = session.open(origin + '/one')
        assert first.title == 'First page'
        assert first.text.startswith('First actual page')
        assert first.untrusted and first.blocked_requests >= 2
        old_ref = first.links[0]['ref']
        inspected = session.inspect()
        with pytest.raises(ValueError, match='current session'):
            session.follow_link(old_ref)
        second = session.follow_link(inspected.links[0]['ref'])
        assert second.final_url == origin + '/two'
        assert second.requested_url == origin + '/two'
        assert second.text == 'Second rendered actual report'
        frame = session.screenshot()
        assert frame.payload.startswith(b'\x89PNG\r\n\x1a\n')
        assert frame.observation_id == second.observation_id
        assert frame.as_dict()['transient'] and 'payload' not in frame.as_dict()
        (tmp_path / 'browser-session.png').write_bytes(frame.payload)
        import json
        (tmp_path / 'browser-session.json').write_text(json.dumps({'first':first.as_dict(), 'second':second.as_dict(), 'screenshot':frame.as_dict()}, indent=2), encoding='utf-8')
        with pytest.raises(ValueError):
            session.follow_link('other:observation:0')
    assert session.closed
    assert '/secret' not in BrowserFixture.hits and '/mutate' not in BrowserFixture.hits


@pytest.mark.browser
@pytest.mark.parametrize('path,error', [('/redirect', 'navigation'), ('/missing', 'HTTP 404'), ('/empty', 'readable text')])
def test_real_chromium_failures_close_session(browser_fixture, path, error):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    session = bs.BrowserSession(scope=scope)
    with pytest.raises(RuntimeError, match=error):
        session.open(origin + path)
    assert session.closed and session._process is None


def test_public_transport_rejects_mixed_dns_before_connect(monkeypatch):
    def mixed(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', mixed)
    with pytest.raises(ValueError, match='non-public'):
        bs._read_pinned('http://mixed.example/two', bs.BrowserScope(), 1000, 1)


def test_public_transport_pins_connection_and_preserves_host(browser_fixture, monkeypatch):
    import socket
    origin, _ = browser_fixture
    port = origin.rsplit(':', 1)[1]
    resolved = []
    real_lookup = socket.getaddrinfo
    def lookup(host, *args, **kwargs):
        resolved.append(host)
        assert host == '127.0.0.1', 'hostname must never be resolved again after validation'
        return real_lookup(host, *args, **kwargs)
    monkeypatch.setattr(bs, 'public_http_addresses', lambda url: ('127.0.0.1',))
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    status, _, payload = bs._read_pinned(f'http://pinned.example:{port}/two', bs.BrowserScope(), 10000, 1)
    assert status == 200 and b'Second rendered actual report' in payload
    assert resolved == ['127.0.0.1']


@pytest.mark.browser
@pytest.mark.parametrize('limits,second_operation', [
    ({'max_pages':1}, 'open'), ({'max_operations':1}, 'inspect'),
    ({'max_response_bytes':100}, 'large'), ({'max_screenshot_bytes':10}, 'screenshot'),
    ({'max_requests':1}, 'requests'),
])
def test_real_chromium_enforces_resource_budgets(browser_fixture, limits, second_operation):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    session = bs.BrowserSession(scope=scope, budget=bs.BrowserSessionBudget(**limits))
    with pytest.raises((ValueError, RuntimeError), match='budget'):
        if second_operation == 'large':
            session.open(origin + '/large')
        elif second_operation == 'requests':
            session.open(origin + '/one')
        else:
            session.open(origin + '/two')
            getattr(session, second_operation)(origin + '/two') if second_operation == 'open' else getattr(session, second_operation)()
    assert session.closed


@pytest.mark.browser
def test_real_chromium_output_truncation_is_explicit(browser_fixture):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    with bs.BrowserSession(scope=scope, budget=bs.BrowserSessionBudget(max_text_chars=10)) as session:
        observation = session.open(origin + '/two')
        assert observation.text == 'Second ren'
        assert observation.truncated


@pytest.mark.browser
@pytest.mark.parametrize('path,limits', [('/auto', {'max_pages':1}), ('/posts', {'max_requests':1})])
def test_real_chromium_counts_page_script_navigation_and_rejected_requests(browser_fixture, path, limits):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    session = bs.BrowserSession(scope=scope, budget=bs.BrowserSessionBudget(**limits))
    with pytest.raises(RuntimeError, match='budget'):
        session.open(origin + path)
    assert session.closed


def _dns_stalled_worker(pipe, scope, budget, session_id):
    import socket
    from pathlib import Path
    original = socket.getaddrinfo
    def stalled(host, *args, **kwargs):
        if host == 'stalled.example':
            Path(os.environ['BROWSER_DNS_MARKER']).write_text('dns entered')
            time.sleep(30)
        return original(host, *args, **kwargs)
    socket.getaddrinfo = stalled
    bs._worker(pipe, scope, budget, session_id)


@pytest.mark.browser
def test_real_chromium_cancel_interrupts_stalled_dns(browser_fixture, monkeypatch, tmp_path):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    marker = tmp_path / 'dns-started'
    monkeypatch.setenv('BROWSER_DNS_MARKER', str(marker))
    monkeypatch.setattr(bs, '_worker', _dns_stalled_worker)
    session = bs.BrowserSession()
    cancelled = []
    def cancel_when_dns_stalls():
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        if marker.exists():
            session.cancel()
            cancelled.append(True)
    thread = threading.Thread(target=cancel_when_dns_stalls)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises((RuntimeError, OSError, ValueError)):
            session.open('http://stalled.example/one')
    finally:
        session.close()
        thread.join(11)
    assert marker.read_text() == 'dns entered'
    assert cancelled and time.monotonic() - started < 10
    assert session.closed and session._process is None


@pytest.mark.browser
@pytest.mark.skipif(os.name != 'nt', reason='Windows target runtime Job Object validation')
def test_real_chromium_close_reaps_entire_windows_process_tree(browser_fixture):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    import ctypes
    from ctypes import wintypes
    import json
    import subprocess
    origin, scope = browser_fixture
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    session = bs.BrowserSession(scope=scope)
    try:
        session.open(origin + '/two')
        root = session._process.pid
        rows = json.loads(subprocess.run(['powershell', '-NoProfile', '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress'], capture_output=True, text=True, check=True, timeout=10).stdout)
        pids = {root}
        for _ in range(10):
            pids.update(row['ProcessId'] for row in rows if row['ParentProcessId'] in pids)
        assert len(pids) >= 4, 'must observe worker, driver and real Chromium descendants'
        for pid in pids:
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                handles.append(handle)
        session.close()
        assert all(kernel.WaitForSingleObject(handle, 2000) == 0 for handle in handles)
    finally:
        session.close()
        for handle in handles:
            kernel.CloseHandle(handle)


@pytest.mark.browser
def test_cancel_serializes_with_process_start(monkeypatch):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    import multiprocessing.context
    start_entered = threading.Event()
    cancel_attempted = threading.Event()
    events = []
    original_start = multiprocessing.context.SpawnProcess.start
    def paused_start(process):
        start_entered.set()
        assert cancel_attempted.wait(3)
        original_start(process)
        events.append('started')
    monkeypatch.setattr(multiprocessing.context.SpawnProcess, 'start', paused_start)
    session = bs.BrowserSession()
    def cancel_during_start():
        assert start_entered.wait(3)
        cancel_attempted.set()
        session.cancel()
        events.append('closed')
    thread = threading.Thread(target=cancel_during_start)
    thread.start()
    try:
        with pytest.raises((RuntimeError, OSError, ValueError)):
            session.inspect()
    finally:
        session.close()
        thread.join(5)
    assert events == ['started', 'closed']
    assert session.closed and session._process is None


@pytest.mark.browser
def test_real_chromium_http_redirect_rechecks_path_and_page_budget(browser_fixture):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, scope = browser_fixture
    with bs.BrowserSession(scope=scope) as session:
        with pytest.raises(RuntimeError, match='navigation'):
            session.open(origin + '/redirect-secret')
    assert '/secret' not in BrowserFixture.hits
    with bs.BrowserSession(scope=scope, budget=bs.BrowserSessionBudget(max_pages=1)) as session:
        with pytest.raises(RuntimeError, match='budget'):
            session.open(origin + '/redirect-two')
    with bs.BrowserSession(scope=scope) as session:
        observation = session.open(origin + '/redirect-two')
        assert observation.requested_url == origin + '/redirect-two'
        assert observation.final_url == origin + '/two'
        assert observation.text == 'Second rendered actual report'


@pytest.mark.browser
def test_real_chromium_links_are_visible_and_never_truncated_targets(browser_fixture):
    if os.getenv('RUN_PLAYWRIGHT') != '1':
        pytest.skip('dedicated real Chromium validation')
    origin, internal_scope = browser_fixture
    scope = bs.BrowserScope(allow_public=True, internal_origins=internal_scope.internal_origins)
    with bs.BrowserSession(scope=scope) as session:
        observation = session.open(origin + '/links')
        assert [link['title'] for link in observation.links] == ['Visible report']
        assert observation.links[0]['url'] == origin + '/two'
