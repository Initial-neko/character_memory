import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from character_memory.llm.client import OpenAICompatibleModel, ProviderHTTPError, StructuredOutputError


def model_with(handler, attempts=2):
    model = OpenAICompatibleModel('fixture-key', attempts=attempts)
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler))
    return model


def valid(request):
    return httpx.Response(200, request=request, json={'choices': [{'message': {'content': '{"actions":[]}'}}]})


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_retry_preserves_request_and_usage(error):
    requests, receipts = [], []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise error('synthetic connection failure', request=request)
        return valid(request)
    model = model_with(handle)
    original_client = model.client
    model._record_usage = lambda **data: receipts.append(data)
    try:
        result = model.react_call_for_session('hello', 'session-one')
        assert result.trace.attempt == 2
        assert requests[0].content == requests[1].content
        assert requests[0].headers == requests[1].headers
        assert model.client is original_client and not original_client.is_closed
        assert [r['status'] for r in receipts] == ['ERROR', 'SUCCESS']
        assert [r['attempt'] for r in receipts] == [1, 2]
        assert len({r['logical_call_id'] for r in receipts}) == 1
    finally:
        model.close()


@pytest.mark.parametrize('attempts', [1, 2, 4])
def test_persistent_connection_failure_is_bounded(attempts):
    requests = []
    def handle(request):
        requests.append(request)
        raise httpx.ConnectError('unavailable', request=request)
    model = model_with(handle, attempts)
    try:
        with pytest.raises(httpx.ConnectError):
            model.react('hello')
        assert len(requests) == attempts
    finally:
        model.close()


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.WriteError])
def test_uncertain_delivery_is_not_replayed(error):
    requests = []
    def handle(request):
        requests.append(request)
        raise error('unknown delivery', request=request)
    model = model_with(handle, 4)
    try:
        with pytest.raises(error): model.react('hello')
        assert len(requests) == 1
    finally:
        model.close()


@pytest.mark.parametrize('status', [401, 429, 503])
def test_http_failure_is_not_replayed(status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, request=request, text='provider unavailable')
    model = model_with(handle, 4)
    try:
        with pytest.raises(ProviderHTTPError): model.react('hello')
        assert len(requests) == 1
    finally:
        model.close()


def test_connection_and_schema_repair_share_attempt_budget():
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1: raise httpx.ConnectError('offline', request=request)
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': '{}'}}]})
    model = model_with(handle, 2)
    try:
        with pytest.raises(StructuredOutputError): model.react('hello')
        assert len(requests) == 2
        assert requests[0].content == requests[1].content
    finally:
        model.close()


def test_real_tcp_refusal_then_recovery_uses_same_client():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"{\\"actions\\":[]}"}}]}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
    # Reserve a real TCP port without listening. The first connection must be refused.
    server.server_bind()
    worker = None
    errors = []
    class RecoveringTransport(httpx.BaseTransport):
        def __init__(self): self.inner = httpx.HTTPTransport(retries=0)
        def handle_request(self, request):
            nonlocal worker
            try: return self.inner.handle_request(request)
            except httpx.ConnectError:
                errors.append('ConnectError')
                server.server_activate()
                worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
                raise
        def close(self): self.inner.close()
    model = OpenAICompatibleModel('fixture-key', base_url=f'http://127.0.0.1:{server.server_address[1]}', timeout=3, attempts=2)
    model.client.close(); model.client = httpx.Client(transport=RecoveringTransport(), timeout=3, trust_env=False)
    try:
        result = model.react_call_for_session('TCP recovery', 'tcp-session')
        assert result.trace.attempt == 2
        assert errors == ['ConnectError']
        assert len(received) == 1
    finally:
        model.close()
        if worker is not None: server.shutdown(); worker.join(timeout=5)
        server.server_close()
