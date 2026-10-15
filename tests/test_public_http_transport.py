"""Transport checks use real HTTP parsing and fake TCP sockets, not a mocked URL guard."""
import socket
import httpx
import pytest
from character_memory.remote_media import RemoteMediaFetcher

PUBLIC = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\nimage"

class Socket:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.closed = False
    def setsockopt(self, *args): pass
    def settimeout(self, value): pass
    def send(self, data):
        self.sent += data
        return len(data)
    def recv(self, size):
        data, self.response = self.response[:size], self.response[size:]
        return data
    def close(self): self.closed = True
    def getpeername(self): return (PUBLIC, 80)
    def fileno(self): return None


def test_rebinding_cannot_change_validated_socket_target(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    resolutions = []
    def resolve(host, port, *args, **kwargs):
        resolutions.append(host)
        address = PUBLIC if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    connected = []
    wire = Socket(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: " + str(len(PNG)).encode() + b"\r\nConnection: close\r\n\r\n" + PNG)
    def connect(address, *args, **kwargs):
        connected.append(address)
        if address[0] == "example.org": resolve(*address)
        return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    fetcher = RemoteMediaFetcher()
    try:
        assert fetcher.fetch_image("http://example.org/image").payload == PNG
    finally:
        fetcher.close()
    assert connected == [(PUBLIC, 80)]
    assert resolutions == ["example.org"]
    assert b"Host: example.org\r\n" in wire.sent


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], [PUBLIC, "10.1.2.3"], ["::1"], ["invalid"], ["100.64.0.1"]])
def test_nonpublic_or_empty_resolution_never_connects(monkeypatch, addresses):
    from character_memory.remote_media import public_http_client
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in addresses
    ])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("unsafe socket connect"))
    with public_http_client(timeout=1) as client:
        with pytest.raises(ValueError): client.get("http://public.example/")


@pytest.mark.parametrize("via_proxy", [False, True])
@pytest.mark.parametrize("hostname,valid", [("public.example", True), ("wrong.example", False)])
def test_real_tls_preserves_sni_and_rejects_wrong_certificate(monkeypatch, via_proxy, hostname, valid):
    import ssl
    import threading
    from character_memory.remote_media import public_http_client
    fixtures = __import__("pathlib").Path(__file__).parent / "fixtures" / "public-http"
    monkeypatch.setenv("SSL_CERT_FILE", str(fixtures / "test-only.crt"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    received = {"sni": [], "requests": [], "errors": []}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixtures / "test-only.crt", fixtures / "test-only.key")
    context.set_servername_callback(lambda sock, name, ctx: received["sni"].append(name))
    def headers(conn):
        data = b""
        while not data.endswith(b"\r\n\r\n"):
            part = conn.recv(4096)
            if not part: break
            data += part
        received["requests"].append(data)
    def serve():
        try:
            with listener:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    if via_proxy:
                        headers(conn)
                        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    with context.wrap_socket(conn, server_side=True) as secure:
                        headers(secure)
                        secure.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        except (ssl.SSLError, OSError) as exc:
            received["errors"].append(str(exc))
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    if via_proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
    else:
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
    original_dns, original_connect = socket.getaddrinfo, socket.create_connection
    connected, resolutions = [], []
    def dns(host, port, *a, **kw):
        if host == hostname:
            resolutions.append(host)
            ip = PUBLIC if len(resolutions) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return original_dns(host, port, *a, **kw)
    def connect(address, *a, **kw):
        connected.append(address)
        assert address[0] == ("127.0.0.1" if via_proxy else PUBLIC)
        return original_connect(("127.0.0.1", port), *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", connect)
    try:
        with public_http_client(timeout=3) as client:
            if valid:
                assert client.get(f"https://{hostname}/tls").text == "OK"
            else:
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                    client.get(f"https://{hostname}/tls")
    finally:
        worker.join(timeout=6)
        listener.close()
    assert not worker.is_alive()
    assert resolutions == [hostname]
    assert received["sni"] == [hostname]
    if via_proxy:
        assert received["requests"][0].startswith(f"CONNECT {PUBLIC}:443 ".encode())
    if valid:
        assert f"Host: {hostname}\r\n".encode() in received["requests"][-1]
        assert not received["errors"]


def test_rss_redirect_rechecks_actual_target_before_connect(monkeypatch, tmp_path):
    from character_memory.rss_sources import RssRepository, RssService
    from character_memory.storage.sqlite import SQLiteStore
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    def dns(host, port, *a, **kw):
        ip = "127.0.0.1" if host == "127.0.0.1" else PUBLIC
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    connected = []
    wire = Socket(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    def connect(address, *a, **kw):
        connected.append(address)
        return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    store = SQLiteStore(tmp_path / "rss.db")
    try:
        service = RssService(RssRepository(store, seed_defaults=False))
        try:
            with pytest.raises(ValueError, match="non-public"):
                service._fetch_text("http://public.example/rss")
        finally: service.close()
    finally: store.close()
    assert connected == [(PUBLIC, 80)]
    assert wire.closed


@pytest.mark.parametrize("no_proxy", ["public.example", "unrelated.example"])
def test_http_proxy_selection_uses_original_hostname(monkeypatch, no_proxy):
    from character_memory.remote_media import public_http_client
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:8123")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    monkeypatch.setenv("no_proxy", no_proxy)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 80))
    ])
    wire = Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    connected = []
    def connect(address, *a, **kw): connected.append(address); return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    with public_http_client(timeout=2) as client:
        assert client.get("http://public.example/item").text == "OK"
    if no_proxy == "public.example":
        assert connected == [(PUBLIC, 80)]
        assert wire.sent.startswith(b"GET /item HTTP/1.1")
    else:
        assert connected == [("127.0.0.1", 8123)]
        assert wire.sent.startswith(f"GET http://{PUBLIC}/item HTTP/1.1".encode())
    assert b"Host: public.example\r\n" in wire.sent
    assert wire.closed


def test_ipv6_connect_failure_falls_back_only_to_validated_ipv4(monkeypatch):
    from character_memory.remote_media import public_http_client
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    ipv6 = "2606:4700:4700::1111"
    resolutions, connected, timeouts = [], [], []
    def dns(host, port, *a, **kw):
        resolutions.append(host)
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ipv6, port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    wire = Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    def connect(address, timeout, *a, **kw):
        connected.append(address); timeouts.append(timeout)
        if address[0] == ipv6: raise OSError("IPv6 unavailable")
        return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    with public_http_client(timeout=2) as client:
        assert client.get("http://public.example/").text == "OK"
    assert resolutions == ["public.example"]
    assert connected == [(ipv6, 80), (PUBLIC, 80)]
    assert 0 < timeouts[1] <= timeouts[0] <= 2


def test_resolution_time_consumes_connect_budget(monkeypatch):
    from character_memory import remote_media
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    clock = [0.0]
    monkeypatch.setattr(remote_media.time, "monotonic", lambda: clock[0])
    def dns(*a, **kw):
        clock[0] += 5
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 80))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("connect budget exhausted"))
    with remote_media.public_http_client(timeout=2) as client:
        with pytest.raises(httpx.ConnectTimeout): client.get("http://public.example/")


def test_avatar_default_downloader_is_pinned(monkeypatch, tmp_path):
    from character_memory.avatars import AvatarStore
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    lookups, connected = [], []
    def dns(host, port, *a, **kw):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC if len(lookups) == 1 else "127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    wire = Socket(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(PNG)).encode() + b"\r\nConnection: close\r\n\r\n" + PNG)
    def connect(address, *a, **kw): connected.append(address); return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    store = AvatarStore(tmp_path / "avatars")
    try:
        assert store.remote_fetcher.fetch_image("http://public.example/avatar").payload == PNG
    finally: store.close()
    assert lookups == ["public.example"]
    assert connected == [(PUBLIC, 80)]



def test_https_proxy_tls_name_is_not_replaced_by_origin_name():
    from character_memory.remote_media import _OriginTLSStream
    calls = []
    class Stream:
        def start_tls(self, context, server_hostname=None, timeout=None):
            calls.append(server_hostname)
            return self
    # Even when proxy and destination share the numeric IP, the first TLS
    # handshake authenticates the proxy. The tunneled handshake authenticates origin.
    wrapped = _OriginTLSStream(Stream(), PUBLIC, "public.example", proxy_tls=True)
    wrapped = wrapped.start_tls(None, PUBLIC)
    wrapped.start_tls(None, PUBLIC)
    assert calls == [PUBLIC, "public.example"]



def test_same_ip_different_hosts_have_separate_tls_connections(monkeypatch):
    import ssl
    from character_memory.remote_media import public_http_client
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))
    ])
    # The fake server keeps the connection readable only when data is sent.
    monkeypatch.setattr("httpcore._backends.sync.is_socket_readable", lambda sock: False)
    wires, authenticated = [], []
    def connect(address, *a, **kw):
        assert address == (PUBLIC, 443)
        # Keepalive is intentionally permitted by the server response.
        wire = Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        wires.append(wire)
        return wire
    def tls(context, wire, server_hostname=None, **kw):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        authenticated.append(server_hostname)
        return wire
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", tls)
    with public_http_client(timeout=2) as client:
        assert client.get("https://one.example/").text == "OK"
        assert wires[0].closed
        assert client.get("https://two.example/").text == "OK"
    assert authenticated == ["one.example", "two.example"]
    assert len(wires) == 2 and all(wire.closed for wire in wires)
    assert b"Host: one.example\r\n" in wires[0].sent
    assert b"Host: two.example\r\n" in wires[1].sent
