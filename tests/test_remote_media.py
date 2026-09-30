import gzip
import socket

import httpx
import pytest

from character_memory.remote_media import RemoteMediaFetcher


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ])


def fetch(stream, headers=None, status=200):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        status, stream=stream, headers=headers or {},
    ))) as client:
        return RemoteMediaFetcher(max_bytes=65536, client=client).fetch_image("https://example.org/image")


def test_chunked_image_stops_at_limit_and_closes_response():
    stream = TrackedStream([b"\x89PNG\r\n\x1a\n" + b"x" * (65536 - 8), b"x" * 65536, b"unread"])
    with pytest.raises(ValueError, match="exceeds"):
        fetch(stream)
    assert stream.read_count == 2
    assert stream.closed


def test_exact_limit_image_succeeds():
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * (65536 - 8)
    stream = TrackedStream([payload[:20], payload[20:]])
    assert fetch(stream).payload == payload
    assert stream.closed


def test_compressed_image_limit_counts_decoded_bytes():
    compressed = gzip.compress(b"\x89PNG\r\n\x1a\n" + b"x" * 131072)
    stream = TrackedStream([compressed])
    with pytest.raises(ValueError, match="exceeds"):
        fetch(stream, {"content-encoding": "gzip", "content-length": str(len(compressed))})
    assert stream.closed


@pytest.mark.parametrize("status,headers,error", [
    (302, {}, "redirect"),
    (500, {}, "HTTP 500"),
    (200, {"content-type": "text/html"}, "content type"),
])
def test_rejected_responses_do_not_read_body(status, headers, error):
    stream = TrackedStream([b"unread"])
    with pytest.raises((RuntimeError, ValueError), match=error):
        fetch(stream, headers, status)
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.parametrize("chunks,error", [([], "empty body"), ([b"html"], "content type")])
def test_empty_or_invalid_image_is_rejected(chunks, error):
    stream = TrackedStream(chunks)
    with pytest.raises((RuntimeError, ValueError), match=error):
        fetch(stream)
    assert stream.closed
