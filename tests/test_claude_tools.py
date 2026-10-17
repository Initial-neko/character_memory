import json
import urllib.error
import urllib.request

import pytest


class Observer:
    def __init__(self): self.calls = []
    def observe(self, query, **limits):
        self.calls.append((query, limits))
        return {"search_results": 1, "fetch_attempts": 1, "observations": [{
            "title": query, "url": "https://example.org/"+query, "source_domain": "example.org",
            "content": "source text"}], "errors": []}


def call(bridge, task, token=None, origin=None):
    headers={"Authorization": "Bearer " + (token or bridge.token), "Content-Type": "application/json"}
    if origin: headers["Origin"]=origin
    request=urllib.request.Request(bridge.url, data=json.dumps(task).encode(), headers=headers)
    return json.loads(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=3).read())


def test_tool_is_bound_to_one_run_and_limits_total_page_attempts():
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    observer=Observer()
    with LocalWorldToolBridge(observer, max_calls=2, max_pages=2, max_chars=500) as bridge:
        first=call(bridge, {"query":"one"})
        second=call(bridge, {"query":"two"})
        assert len(first["observations"]) == len(second["observations"]) == 1
        assert first["receipts"][0]["content_scope"] == "WEB_EXCERPT"
        assert bridge.observations[0]["content"] == "source text"
        with pytest.raises(urllib.error.HTTPError) as error: call(bridge, {"query":"three"})
        assert error.value.code == 429
    assert len(observer.calls) == 2


@pytest.mark.parametrize("payload,token,origin,status", [
    ({"query":"one"},"wrong",None,403),
    ({"query":"one"},None,"https://example.org",403),
    ({"query":"one","character_id":"other"},None,None,400),
    ({"query":""},None,None,400),
])
def test_callback_rejects_untrusted_identity_and_credentials(payload, token, origin, status):
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    observer=Observer()
    with LocalWorldToolBridge(observer) as bridge:
        with pytest.raises(urllib.error.HTTPError) as error: call(bridge,payload,token,origin)
        assert error.value.code == status
    assert not observer.calls


def test_mcp_only_exposes_the_single_read_only_tool():
    from character_memory.runtime.claude_tools import mcp_response
    initialized=mcp_response({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}})
    assert initialized["result"]["capabilities"] == {"tools": {}}
    tools=mcp_response({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    assert [tool["name"] for tool in tools["result"]["tools"]] == ["world_lookup"]
    denied=mcp_response({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"shell","arguments":{}}})
    assert "error" in denied
    assert mcp_response({"jsonrpc":"2.0","method":"notifications/initialized"}) is None


def test_real_tool_steps_are_bounded_sourced_and_do_not_include_invalid_input():
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    observer = Observer()
    bridge = LocalWorldToolBridge(observer, max_calls=1, request_id='request:one')
    assert bridge.steps == []
    result = bridge.execute({'query': 'one'})
    with pytest.raises(PermissionError):
        bridge.execute({'query': 'two'})
    for _ in range(10):
        with pytest.raises(ValueError):
            bridge.execute({'query': 'SECRET', 'token': 'PRIVATE'})
    assert len(bridge.steps) == 2
    success, denied = bridge.steps
    assert success['request_id'] == 'request:one'
    assert success['tool'] == 'world_lookup' and success['arguments'] == {'query': 'one'}
    assert success['status'] == 'SUCCESS'
    assert success['result']['observations'][0]['content'] == 'source text'
    assert success['result']['receipts'] == result['receipts']
    assert success['started_at'] <= success['completed_at'] and success['elapsed_ms'] >= 0
    assert denied['status'] == 'DENIED' and denied['reason'] == 'TOOL_BUDGET_EXHAUSTED'
    assert 'SECRET' not in json.dumps(bridge.steps) and 'PRIVATE' not in json.dumps(bridge.steps)


def test_tool_step_failure_and_truncation_are_explicit():
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    class Failing:
        def observe(self, *args, **kwargs):
            raise RuntimeError('secret provider environment')
    bridge = LocalWorldToolBridge(Failing(), request_id='failed')
    with pytest.raises(RuntimeError):
        bridge.execute({'query': 'test'})
    assert bridge.steps[0]['status'] == 'FAILED'
    assert bridge.steps[0]['reason'] == 'TOOL_EXECUTION_FAILED'
    assert 'secret provider environment' not in json.dumps(bridge.steps)
    class Long(Observer):
        def observe(self, *args, **kwargs):
            result = super().observe(*args, **kwargs)
            result['observations'][0]['content'] = 'x'*6000
            return result
    bridge = LocalWorldToolBridge(Long(), request_id='long')
    bridge.execute({'query': 'long'})
    row = bridge.steps[0]['result']['observations'][0]
    assert len(row['content']) == 1200 and row['excerpt_truncated'] is True
    assert row['available_characters'] == 6000


def test_incomplete_step_is_unknown_and_late_cancelled_result_is_not_success():
    import threading
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    entered, release = threading.Event(), threading.Event()
    class Slow(Observer):
        def observe(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().observe(*args, **kwargs)
    bridge = LocalWorldToolBridge(Slow(), request_id='pending')
    errors = []
    def run():
        try: bridge.execute({'query':'slow'})
        except PermissionError: errors.append('cancelled')
    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(3)
    snapshot = list(bridge.steps)
    assert snapshot[0]['status'] == 'RUNNING'
    assert snapshot[0]['completed_at'] is None and snapshot[0]['result'] is None
    bridge.closed.set()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and errors == ['cancelled']
    assert bridge.steps[0]['status'] == 'FAILED' and bridge.steps[0]['reason'] == 'TOOL_CANCELLED'
    assert bridge.observations == []
    assert snapshot[0]['status'] == 'RUNNING'


def test_step_credential_redaction_happens_before_excerpt_cutoff():
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    class Secret(Observer):
        def observe(self, *args, **kwargs):
            data = super().observe(*args, **kwargs)
            data['observations'][0]['content'] = 'x'*1195+'test-secret'+'tail'
            return data
    bridge = LocalWorldToolBridge(Secret())
    bridge.redact = lambda value: value.replace('test-secret', '[REDACTED]')
    bridge.execute({'query':'test-secret'})
    step = bridge.steps[0]
    assert step['arguments']['query']=='[REDACTED]'
    assert step['result']['observations'][0]['content'].endswith('[REDA')
    assert 'test-' not in step['result']['observations'][0]['content']


def test_mcp_stdio_returns_utf8_sources_under_legacy_console_encoding():
    """The real child must survive source characters absent from Windows GBK."""
    import os
    import subprocess
    import sys
    from character_memory.runtime.claude_tools import LocalWorldToolBridge

    source = 'dataclasses \u2014 \u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac \u65e5\u672c\u8a9e \U0001f4da'

    class UnicodeObserver(Observer):
        def observe(self, query, **limits):
            data = super().observe(query, **limits)
            data['observations'][0]['content'] = source
            return data

    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'world_lookup', 'arguments': {'query': 'documentation'}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
    ]
    with LocalWorldToolBridge(UnicodeObserver()) as bridge:
        env = {**os.environ, 'CM_CLAUDE_CALLBACK': bridge.url,
               'CM_CLAUDE_TOKEN': bridge.token, 'PYTHONIOENCODING': 'gbk', 'PYTHONUTF8': '0'}
        child = subprocess.run([sys.executable, '-m', 'character_memory.runtime.claude_tools'],
            input=('\n'.join(json.dumps(m) for m in messages)+'\n').encode('utf-8'),
            capture_output=True, env=env, timeout=15)
    assert child.returncode == 0, child.stderr.decode('ascii', errors='replace')
    replies = [json.loads(line) for line in child.stdout.decode('utf-8').splitlines()]
    assert [r['id'] for r in replies] == [1, 2]
    assert replies[0]['result']['isError'] is False
    lookup = json.loads(replies[0]['result']['content'][0]['text'])
    assert lookup['observations'][0]['content'] == source
    assert replies[1]['result'] == {}
