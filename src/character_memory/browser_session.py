"""Bounded, disposable read-only Chromium sessions; no user browser state.

Only this module's fixed extraction script executes. All HTTP traffic is fulfilled
from pinned sockets, so Chromium cannot re-resolve a validated public hostname.
The process boundary makes even a stalled OS DNS lookup cancellable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import http.client
import ipaddress
import multiprocessing
import os
import signal
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid

from character_memory.remote_media import public_http_addresses


def _url_parts(url: str):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ValueError('invalid browser URL')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('browser URL must be credential-free HTTP(S)')
    if '\\' in url or '%' in parsed.netloc:
        raise ValueError('ambiguous browser URL')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    host = parsed.hostname.lower()
    origin = f'{parsed.scheme}://{("[" + host + "]") if ":" in host else host}:{port}'
    return parsed, origin


@dataclass(frozen=True)
class BrowserInternalOrigin:
    """Server-supplied read-only view routes, never model-supplied authorization."""
    origin: str
    allowed_paths: tuple[str, ...]

    def __post_init__(self):
        parsed, origin = _url_parts(self.origin)
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('internal origin must have no path/query/fragment')
        if not self.allowed_paths or any(not p.startswith('/') or any(c in p for c in ('%', '\\', '?', '#')) or any(s in ('.', '..') for s in p.split('/')) for p in self.allowed_paths):
            raise ValueError('internal paths must be exact canonical read-only paths')
        object.__setattr__(self, 'origin', origin)
        object.__setattr__(self, 'allowed_paths', tuple(self.allowed_paths))


@dataclass(frozen=True)
class BrowserScope:
    allow_public: bool = True
    internal_origins: tuple[BrowserInternalOrigin, ...] = ()

    def classify(self, url: str) -> str:
        parsed, origin = _url_parts(url)
        for grant in self.internal_origins:
            if origin == grant.origin:
                if parsed.query or parsed.path not in grant.allowed_paths:
                    raise ValueError('internal path is outside read-only scope')
                return 'internal'
        if not self.allow_public:
            raise ValueError('origin is outside browser scope')
        host = parsed.hostname.lower()
        if host in ('localhost', 'localhost.localdomain') or host.endswith('.local'):
            raise ValueError('private browser target')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return 'public'  # DNS is checked and pinned in the cancellable worker.
        if not address.is_global or address.is_multicast:
            raise ValueError('private browser target')
        return 'public'


@dataclass(frozen=True)
class BrowserSessionBudget:
    max_operations: int = 12
    max_pages: int = 4
    max_seconds: float = 60
    operation_timeout_seconds: float = 15
    max_text_chars: int = 12000
    max_links: int = 40
    max_response_bytes: int = 4 * 1024 * 1024
    max_screenshot_bytes: int = 2 * 1024 * 1024
    max_requests: int = 80

    def __post_init__(self):
        ceilings = {'max_operations': 100, 'max_pages': 20, 'max_seconds': 300,
                    'operation_timeout_seconds': 90, 'max_text_chars': 50000,
                    'max_links': 100, 'max_response_bytes': 16 * 1024 * 1024,
                    'max_screenshot_bytes': 4 * 1024 * 1024, 'max_requests': 200}
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= ceiling:
                raise ValueError(f'invalid browser budget: {name}')
            if name not in ('max_seconds', 'operation_timeout_seconds') and not isinstance(value, int):
                raise ValueError(f'browser budget must be integer: {name}')


@dataclass(frozen=True)
class BrowserObservation:
    session_id: str
    observation_id: str
    requested_url: str
    final_url: str
    title: str
    text: str
    truncated: bool
    observed_at: str
    links: tuple[dict[str, str], ...]
    untrusted: bool = True
    blocked_requests: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BrowserScreenshot:
    payload: bytes = field(repr=False)
    session_id: str = ''
    observation_id: str = ''
    source_url: str = ''
    observed_at: str = ''
    content_type: str = 'image/png'
    width: int = 1280
    height: int = 720

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != 'payload'} | {'byte_length': len(self.payload), 'transient': True}


_SESSION_SLOTS = threading.BoundedSemaphore(2)


class BrowserSession:
    """One owning thread, one bounded worker. close/cancel may cross threads.

    A failure, expiry or cancellation permanently closes this session; create a
    fresh instance to restart. No cookies, grants or screenshot bytes persist.
    """
    def __init__(self, *, budget: BrowserSessionBudget | None = None, scope: BrowserScope | None = None):
        self.budget = budget or BrowserSessionBudget()
        self.scope = scope or BrowserScope()
        self.session_id = uuid.uuid4().hex
        self._owner = threading.get_ident()
        self._started = time.monotonic()
        self._operations = 0
        self._closed = False
        self._process = None
        self._pipe = None
        self._tree = None
        self._cleanup_lock = threading.Lock()
        self._observation = None
        if not _SESSION_SLOTS.acquire(blocking=False):
            raise RuntimeError('browser concurrent session limit reached')
        self._slot_owned = True
        self._timer = threading.Timer(self.budget.max_seconds, self.close)
        self._timer.daemon = True
        self._timer.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def _check(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('browser session belongs to another thread')
        if self._closed:
            raise RuntimeError('browser session is closed')
        if time.monotonic() - self._started >= self.budget.max_seconds or self._operations >= self.budget.max_operations:
            self.close()
            raise RuntimeError('browser session budget exhausted')

    def _call(self, operation: str, value=None):
        self._check()
        self._operations += 1
        deadline = min(self._started + self.budget.max_seconds, time.monotonic() + self.budget.operation_timeout_seconds)
        try:
            with self._cleanup_lock:
                if self._closed:
                    raise RuntimeError('browser session cancelled')
                if self._process is None:
                    ctx = multiprocessing.get_context('spawn')
                    self._pipe, child = ctx.Pipe()
                    try:
                        self._process = ctx.Process(target=_worker, args=(child, self.scope, self.budget, self.session_id), daemon=True)
                        if os.name == 'nt':
                            from character_memory.runtime.process_tree import WindowsProcessTree
                            self._tree = WindowsProcessTree()
                        self._process.start()
                    finally:
                        child.close()
                    if self._tree is not None and not self._tree.kernel.AssignProcessToJobObject(self._tree.handle, self._process.sentinel):
                        raise RuntimeError('browser process tree isolation failed')
                    self._pipe.send(('start', None))
                pipe = self._pipe
            pipe.send((operation, value))
            while time.monotonic() < deadline:
                if self._closed:
                    raise RuntimeError('browser session cancelled')
                if pipe.poll(min(.05, max(0, deadline - time.monotonic()))):
                    ok, result = pipe.recv()
                    if not ok:
                        raise RuntimeError(result)
                    return result
            raise TimeoutError('browser session operation deadline exceeded')
        except BaseException:
            self.close()
            raise

    def open(self, url: str) -> BrowserObservation:
        self._check()
        self.scope.classify(url)
        self._observation = self._call('open', url)
        return self._observation

    def inspect(self) -> BrowserObservation:
        self._observation = self._call('inspect')
        return self._observation

    def follow_link(self, link_ref: str) -> BrowserObservation:
        self._check()
        if not isinstance(link_ref, str) or not self._observation or not any(x['ref'] == link_ref for x in self._observation.links):
            raise ValueError('link reference is not from the current session observation')
        self._observation = self._call('follow_link', link_ref)
        return self._observation

    def screenshot(self) -> BrowserScreenshot:
        return self._call('screenshot')

    def close(self) -> None:
        with self._cleanup_lock:
            self._closed = True
            self._timer.cancel()
            process, pipe = self._process, self._pipe
            try:
                if process is not None and process.pid is not None:
                    if process.is_alive():
                        try:
                            pipe.send(('close', None))
                            process.join(.3)
                        except (OSError, EOFError):
                            pass
                    # The Job also owns descendants whose worker already exited.
                    if self._tree is not None:
                        self._tree.close()
                    elif os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.is_alive():
                        process.kill()
                    process.join(2)
                    if not process.is_alive():
                        process.close()
            finally:
                if self._tree is not None:
                    self._tree.close()
                if pipe is not None:
                    pipe.close()
                self._process = self._pipe = self._tree = None
                self._observation = None
                if self._slot_owned:
                    self._slot_owned = False
                    _SESSION_SLOTS.release()

    cancel = close

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self.close()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_pinned(url: str, scope: BrowserScope, remaining_bytes: int, timeout: float):
    """GET on a validated numeric socket, with original Host and TLS identity."""
    kind = scope.classify(url)
    parsed, _ = _url_parts(url)
    if kind == 'public':
        addresses = public_http_addresses(url)
    else:
        addresses = tuple(x[4][0] for x in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM))
    if not addresses:
        raise ValueError('browser host resolved to no addresses')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    connection = http.client.HTTPConnection(parsed.hostname, port=port, timeout=timeout)
    try:
        connection.sock = socket.create_connection((addresses[0], port), timeout=timeout)
        if parsed.scheme == 'https':
            connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
        connection.request('GET', urlunsplit(('', '', parsed.path or '/', parsed.query, '')), headers={'Host': parsed.netloc, 'Accept-Encoding': 'identity', 'User-Agent': 'CharacterMemoryBrowserSession/1.0'})
        response = connection.getresponse()
        if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
            raise ValueError('compressed browser responses are not accepted')
        payload = response.read(remaining_bytes + 1)
        if len(payload) > remaining_bytes:
            raise ValueError('browser response byte budget exhausted')
        headers = {k.lower(): v for k, v in response.getheaders() if k.lower() not in ('set-cookie', 'content-length', 'transfer-encoding', 'connection')}
        if 'attachment' in headers.get('content-disposition', '').lower():
            raise ValueError('browser downloads are forbidden')
        return response.status, headers, payload
    finally:
        connection.close()


_EXTRACT = """limits => {
 const text = (document.body?.innerText || '').trim();
 const links = Array.from(document.querySelectorAll('a[href]'))
   .filter(a => a.href.length <= 4096 && a.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}) && (a.innerText || '').trim())
   .slice(0, limits.links).map(a => ({url:a.href, title:a.innerText.trim().slice(0,240)}));
 return {title:document.title.slice(0,500), text:text.slice(0,limits.text), truncated:text.length>limits.text, links};
}"""


def _worker(pipe, scope: BrowserScope, budget: BrowserSessionBudget, session_id: str):
    if os.name != 'nt':
        os.setsid()
    # No browser/driver child is created before the parent attaches our Job.
    if pipe.recv() != ('start', None):
        pipe.close()
        return
    browser = context = None
    playwright = None
    try:
        from playwright.sync_api import sync_playwright
        from character_memory.browser_web import HeadlessBrowserWebFetcher
        playwright = sync_playwright().start()
        browser = HeadlessBrowserWebFetcher()._launch(playwright, launch_args=[
            '--host-resolver-rules=MAP * ~NOTFOUND',
            '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-quic', '--disable-background-networking',
        ])
        context = browser.new_context(viewport={'width': 1280, 'height': 720}, service_workers='block', accept_downloads=False)
        state = {'bytes': 0, 'requests': 0, 'pages': 0, 'blocked': 0, 'fatal': None, 'navigation_origin': None, 'navigation_kind': None, 'redirect': None}

        def route_guard(route):
            try:
                url = route.request.url
                main_document = route.request.is_navigation_request() and route.request.frame == page.main_frame
                while True:
                    state['requests'] += 1
                    if state['requests'] > budget.max_requests:
                        raise ValueError('browser request budget exhausted')
                    if route.request.method != 'GET':
                        raise ValueError('browser read-only policy rejected request')
                    if main_document:
                        state['pages'] += 1
                        if state['pages'] > budget.max_pages:
                            raise ValueError('browser page budget exhausted')
                    kind = scope.classify(url)
                    _, target_origin = _url_parts(url)
                    if (kind == 'internal' or state['navigation_kind'] == 'internal') and (kind != state['navigation_kind'] or target_origin != state['navigation_origin']):
                        raise ValueError('cross-scope browser request rejected')
                    status, headers, payload = _read_pinned(url, scope, budget.max_response_bytes - state['bytes'], budget.operation_timeout_seconds)
                    state['bytes'] += len(payload)
                    if 300 <= status < 400:
                        if not headers.get('location'):
                            raise ValueError('browser redirect has no destination')
                        target = urljoin(url, headers['location'])
                        scope.classify(target)
                        if main_document:
                            # Chromium does not re-route a fulfill(302) destination.
                            # Finish an empty intermediary (never observed), then
                            # our owning loop navigates the next guarded hop.
                            state['redirect'] = target
                            route.fulfill(status=200, content_type='text/html', body='<!doctype html>')
                            return
                        url = target
                        continue
                    if main_document and status >= 400:
                        raise ValueError(f'browser navigation failed: HTTP {status}')
                    route.fulfill(status=status, headers=headers, body=payload)
                    return
            except Exception as exc:
                state['blocked'] += 1
                if route.request.is_navigation_request() or 'budget' in str(exc):
                    state['fatal'] = str(exc)[:500]
                route.abort()

        context.route('**/*', route_guard)
        if not hasattr(context, 'route_web_socket'):
            raise RuntimeError('browser lacks required WebSocket blocking support')
        context.route_web_socket('**/*', lambda ws: ws.close())
        page = context.new_page()
        context.on('page', lambda extra: extra.close() if extra != page else None)
        observation = None
        requested_url = ''
        while True:
            operation, value = pipe.recv()
            if operation == 'close':
                break
            try:
                if operation in ('open', 'follow_link'):
                    if state['pages'] >= budget.max_pages:
                        raise ValueError('browser page budget exhausted')
                    if operation == 'follow_link':
                        match = next((link for link in observation.links if link['ref'] == value), None) if observation else None
                        if match is None:
                            raise ValueError('stale browser link reference')
                        value = match['url']
                    state['navigation_kind'] = scope.classify(value)
                    _, state['navigation_origin'] = _url_parts(value)
                    requested_url = value
                    state['fatal'] = None
                    target = value
                    while True:
                        state['redirect'] = None
                        try:
                            response = page.goto(target, wait_until='domcontentloaded', timeout=int(budget.operation_timeout_seconds * 1000))
                        except Exception as exc:
                            if state['redirect']:
                                target = state['redirect']
                                continue
                            raise ValueError(f'browser navigation failed: {state["fatal"] or exc}') from exc
                        page.wait_for_timeout(150)
                        if state['redirect']:
                            target = state['redirect']
                            continue
                        if state['fatal']:
                            raise ValueError(state['fatal'])
                        if response is None or response.status >= 400:
                            raise ValueError(f'browser navigation failed: HTTP {response.status if response else "missing"}')
                        break
                    operation = 'inspect'
                if state['fatal']:
                    raise ValueError(state['fatal'])
                if operation == 'inspect':
                    if not requested_url:
                        raise ValueError('browser has no open page')
                    scope.classify(page.url)
                    extracted = page.evaluate(_EXTRACT, {'text': budget.max_text_chars, 'links': budget.max_links})
                    if state['fatal'] or state['redirect']:
                        raise ValueError(state['fatal'] or 'browser navigation changed during observation')
                    if not extracted['text']:
                        raise ValueError('browser page has no readable text')
                    version = uuid.uuid4().hex
                    links = []
                    for item in extracted['links']:
                        try:
                            scope.classify(item['url'])
                        except ValueError:
                            continue
                        links.append({'ref': f'{session_id}:{version}:{len(links)}', **item})
                    observation = BrowserObservation(session_id, version, requested_url, page.url, extracted['title'], extracted['text'], extracted['truncated'], _utc(), tuple(links), blocked_requests=state['blocked'])
                    pipe.send((True, observation))
                elif operation == 'screenshot':
                    if not observation or page.url != observation.final_url:
                        raise ValueError('browser screenshot has no current observation')
                    payload = page.screenshot(type='png', full_page=False, timeout=int(budget.operation_timeout_seconds * 1000))
                    if state['fatal'] or state['redirect'] or page.url != observation.final_url:
                        raise ValueError(state['fatal'] or 'browser navigation changed during screenshot')
                    if len(payload) > budget.max_screenshot_bytes:
                        raise ValueError('browser screenshot byte budget exhausted')
                    pipe.send((True, BrowserScreenshot(payload, session_id, observation.observation_id, page.url, _utc())))
                else:
                    raise ValueError('unsupported browser operation')
            except Exception as exc:
                pipe.send((False, f'{type(exc).__name__}: {str(exc)[:600]}'))
                break
    except Exception as exc:
        try:
            pipe.send((False, f'{type(exc).__name__}: {str(exc)[:600]}'))
        except (OSError, EOFError):
            pass
    finally:
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except Exception:
                    pass
        pipe.close()
