from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlparse

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


def ensure_public_http_url(url: str) -> None:
    """Reject local/private targets before any server-side media download."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("media URL must be an absolute http(s) URL")
    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("media URL points to a local host")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError(f"media host could not be resolved: {hostname}") from exc

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("media URL resolved to a non-public address")


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
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def fetch_image(self, url: str) -> RemoteMedia:
        ensure_public_http_url(url)
        response = self.client.get(
            url,
            headers={"Accept": "image/*", "User-Agent": "character-memory/0.13 remote-media"},
            follow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise RuntimeError("remote image redirect was rejected for safety")
        if response.is_error:
            raise RuntimeError(f"remote image download failed with HTTP {response.status_code}")

        payload = bytes(response.content or b"")
        if not payload:
            raise RuntimeError("remote image download returned an empty body")
        if len(payload) > self.max_bytes:
            raise ValueError(f"remote image exceeds the {self.max_bytes} byte limit")

        header_mime = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        sniffed = _sniff_image_mime(payload)
        if sniffed is None:
            raise ValueError(f"unsupported remote image content type: {header_mime or '<missing>'}")
        if header_mime and header_mime not in _IMAGE_MIME:
            raise ValueError(f"unsupported remote image content type: {header_mime}")
        return RemoteMedia(payload=payload, content_type=sniffed, source_url=url)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
