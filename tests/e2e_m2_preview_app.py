"""Opt-in loopback preview of the real M2 UI with isolated data and fixed replies.

Set M2_PREVIEW_ROOT and the three CHARACTER_MEMORY_E2E paths underneath it.
Run with uvicorn e2e_m2_preview_app:app --app-dir tests --host 127.0.0.1.
No provider credentials are needed or loaded by this fixture.
"""
import os
from pathlib import Path
import socket

root = Path(os.environ['M2_PREVIEW_ROOT']).resolve(strict=True)
for name in ('CHARACTER_MEMORY_E2E_DB', 'CHARACTER_MEMORY_E2E_MEDIA', 'CHARACTER_MEMORY_E2E_PERSONA_ROOT'):
    value = Path(os.environ[name]).resolve()
    if not value.is_relative_to(root) or value == root:
        raise RuntimeError('preview data must be strictly under M2_PREVIEW_ROOT')
os.environ['M2_FULL_LOOP_STORY'] = '1'
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_lookup = socket.getaddrinfo

def _local_address(address):
    if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}:
        raise OSError('external connections disabled in fixed M2 preview')

def _connect(sock, address):
    _local_address(address)
    return _original_connect(sock, address)

def _connect_ex(sock, address):
    _local_address(address)
    return _original_connect_ex(sock, address)

def _lookup(host, *args, **kwargs):
    if host not in {'127.0.0.1', '::1', 'localhost', b'127.0.0.1', b'::1', None}:
        raise OSError('external DNS disabled in fixed M2 preview')
    return _original_lookup(host, *args, **kwargs)
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _lookup

from fastapi.responses import HTMLResponse, JSONResponse
from m2_preview_policy import allowed_request
from e2e_suggestions_ui_app import app

@app.middleware('http')
async def preview_boundary(request, call_next):
    if not allowed_request(request.method, request.url.path, request.headers.get('host', ''), request.headers.get('origin')):
        return JSONResponse({'detail': 'This isolated preview supports only the M2 chat loop.'}, status_code=403)
    response = await call_next(request)
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'none'"
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/preview', response_class=HTMLResponse)
def preview():
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>M2 隔离体验</title><style>html,body{margin:0;height:100%;font:14px system-ui;color:#33205e;background:#f6f2ff}body{display:flex;flex-direction:column}header{padding:8px 12px;line-height:1.5;border-bottom:1px solid #ddd1ff}iframe{border:0;width:100%;flex:1;min-height:0;background:white}</style><header><strong>隔离体验 · 固定本地回复</strong>　不代表真实模型质量；可测试偏好、建议、发送与反馈。<br>仅保存到独立测试数据；World、工具、配置、通话和外部连接已禁用。</header><iframe src="/" title="Character Memory M2 聊天体验"></iframe></html>'''