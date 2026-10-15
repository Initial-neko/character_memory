from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpcore
import httpx


_IMAGE_MIME = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


@dataclass(frozen=True)
class RemoteMedia:
    payload: bytes
    content_type: str
    source_url: str


def public_http_addresses(url: str) -> tuple[str, ...]:
    """Reject local/private targets before any server-side media download."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("media URL must be an absolute http(s) URL")
    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("media URL points to a local host")
    try:
        addresses = tuple(dict.fromkeys(
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        ))
    except OSError as exc:
        raise ValueError(f"media host could not be resolved: {hostname}") from exc

    if not addresses:
        raise ValueError("media host resolved to no addresses")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("media host resolved to an invalid address") from exc
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("media URL resolved to a non-public address")

    return addresses


def ensure_public_http_url(url: str) -> None:
    public_http_addresses(url)


class _OriginTLSStream(httpcore.NetworkStream):
    """Keep the origin's TLS name when CONNECT targets a pinned numeric IP."""
    def __init__(self, stream, address, hostname, proxy_tls=False):
        self.stream, self.address, self.hostname = stream, address, hostname
        self.proxy_tls = proxy_tls

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout)

    def close(self):
        self.stream.close()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # The proxy's own TLS name must remain unchanged. Only the pinned
        # destination name is restored, including httpcore's CONNECT path.
        name = self.hostname if not self.proxy_tls and server_hostname == self.address else server_hostname
        stream = self.stream.start_tls(ssl_context, server_hostname=name, timeout=timeout)
        return _OriginTLSStream(stream, self.address, self.hostname)


class _OriginTLSBackend(httpcore.NetworkBackend):
    def __init__(self, address, hostname, proxy_tls=False):
        self.proxy_tls = proxy_tls
        self.backend = httpcore.SyncBackend()
        self.address, self.hostname = address, hostname

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = self.backend.connect_tcp(host, port, timeout, local_address, socket_options)
        return _OriginTLSStream(stream, self.address, self.hostname, self.proxy_tls)


class _OwnedResponseStream(httpx.SyncByteStream):
    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    def __iter__(self):
        yield from self.stream

    def close(self):
        try:
            self.stream.close()
        finally:
            self.transport.close()


class PublicHTTPTransport(httpx.BaseTransport):
    """Resolve once per request; connect/CONNECT only to validated IPs.

    A per-request pool prevents cross-host TLS reuse when hosts share an IP.
    HTTPX 0.28.1's proxy matcher and network-backend slot are isolated here;
    transport tests are the upgrade contract for these two private seams.
    """
    def __init__(self):
        from httpx._utils import URLPattern, get_environment_proxies
        self.proxies = sorted(
            ((URLPattern(pattern), proxy) for pattern, proxy in get_environment_proxies().items()),
            key=lambda pair: pair[0],
        )
        self.ssl_context = httpx.create_ssl_context()

    def handle_request(self, request):
        started = time.monotonic()
        addresses = public_http_addresses(str(request.url))
        proxy = next((proxy for pattern, proxy in self.proxies if pattern.matches(request.url)), None)
        timeouts = dict(request.extensions.get("timeout", {}))
        for index, address in enumerate(addresses):
            remaining = dict(timeouts)
            connect_timeout = timeouts.get("connect")
            if connect_timeout is not None:
                remaining["connect"] = connect_timeout - (time.monotonic() - started)
                if remaining["connect"] <= 0:
                    raise httpx.ConnectTimeout("public HTTP connect deadline exceeded", request=request)
            transport = httpx.HTTPTransport(verify=self.ssl_context, proxy=proxy)
            # HTTPX does not expose network_backend. Use httpcore's documented
            # backend interface without replacing certificate verification.
            if not hasattr(transport._pool, "_network_backend"):
                transport.close()
                raise RuntimeError("unsupported HTTP transport backend; refusing unpinned request")
            transport._pool._network_backend = _OriginTLSBackend(
                address, request.url.host, proxy_tls=bool(proxy and httpx.URL(proxy).scheme == "https"),
            )
            pinned = httpx.Request(
                request.method, request.url.copy_with(host=address),
                headers=request.headers, stream=request.stream,
                extensions={**request.extensions, "timeout": remaining},
            )
            try:
                response = transport.handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                transport.close()
                if index + 1 == len(addresses):
                    raise
                continue
            except BaseException:
                transport.close()
                raise
            response.stream = _OwnedResponseStream(response.stream, transport)
            return response
        raise RuntimeError("no validated HTTP address")


def public_http_client(**kwargs) -> httpx.Client:
    # This transport selects the original hostname's HTTP_PROXY/NO_PROXY
    # route before pinning. Disable Client's automatic mounts to avoid bypass.
    return httpx.Client(transport=PublicHTTPTransport(), trust_env=False, **kwargs)


def _sniff_image_mime(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


class RemoteMediaFetcher:
    """Small SSRF-safe downloader for durable remote image assets.

    Redirects are deliberately rejected so every network target is validated
    before the request. Callers own product-specific selection/ranking; this
    component only enforces transport and media safety.
    """

    def __init__(
        self,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self._owns_client = client is None
        self.client = client or public_http_client(timeout=timeout, follow_redirects=False)

    def fetch_image(self, url: str) -> RemoteMedia:
        if not self._owns_client:
            ensure_public_http_url(url)
        with self.client.stream(
            "GET",
            url,
            headers={"Accept": "image/*", "User-Agent": "character-memory/0.13 remote-media"},
            follow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise RuntimeError("remote image redirect was rejected for safety")
            if response.is_error:
                raise RuntimeError(f"remote image download failed with HTTP {response.status_code}")

            header_mime = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if header_mime and header_mime not in _IMAGE_MIME:
                raise ValueError(f"unsupported remote image content type: {header_mime}")
            payload_buffer = bytearray()
            # Count decoded bytes too: a compressed or chunked response must
            # obey the same limit even when Content-Length is absent or small.
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(payload_buffer) + len(chunk) > self.max_bytes:
                    raise ValueError(f"remote image exceeds the {self.max_bytes} byte limit")
                payload_buffer.extend(chunk)
            payload = bytes(payload_buffer)
        if not payload:
            raise RuntimeError("remote image download returned an empty body")
        sniffed = _sniff_image_mime(payload)
        if sniffed is None:
            raise ValueError(f"unsupported remote image content type: {header_mime or '<missing>'}")
        return RemoteMedia(payload=payload, content_type=sniffed, source_url=url)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
