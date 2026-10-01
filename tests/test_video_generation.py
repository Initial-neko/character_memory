import json

import httpx
import pytest

from character_memory.video_generation import (
    MetasoMiniMaxVideoProvider,
    VideoGenerationRequest,
)


MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


def test_minimax_h3_create_poll_and_download_never_forwards_bearer_to_cdn():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/api/minimax/v2/video_generation":
            assert request.headers["authorization"] == "Bearer test-key"
            assert json.loads(request.content) == {
                "model": "MiniMax-H3",
                "content": [{"type": "text", "text": "雨夜里向前走，镜头缓慢跟随。"}],
                "resolution": "2K",
                "duration": 5,
                "ratio": "16:9",
            }
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/api/minimax/v2/query/video_generation/task-1":
            assert request.headers["authorization"] == "Bearer test-key"
            return httpx.Response(
                200,
                json={
                    "task": {
                        "status": "success",
                        "content": {"url": "https://cdn.example.test/video.mp4"},
                    }
                },
            )
        if request.url.host == "cdn.example.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = MetasoMiniMaxVideoProvider(
        "test-key",
        base_url="https://metaso.example.test/api/minimax",
        client=client,
        sleep=lambda _seconds: None,
        url_validator=lambda _url: None,
    )
    result = provider.generate(
        VideoGenerationRequest(
            prompt="雨夜里向前走，镜头缓慢跟随。",
            resolution="2K",
            duration_seconds=5,
            ratio="16:9",
        )
    )

    assert result.task_id == "task-1"
    assert result.payload == MP4
    assert result.mime_type == "video/mp4"
    assert [request.method for request in calls] == ["POST", "GET", "GET"]
    client.close()


def test_minimax_h3_error_redacts_bearer_shaped_secret():
    secret = "mk-THISMUSTNOTAPPEAR123456"

    def handler(_request: httpx.Request):
        return httpx.Response(401, text=f"Authorization: Bearer {secret}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = MetasoMiniMaxVideoProvider(
        secret,
        base_url="https://metaso.example.test/api/minimax",
        client=client,
        url_validator=lambda _url: None,
    )

    with pytest.raises(RuntimeError) as exc:
        provider.generate(VideoGenerationRequest(prompt="test"))
    assert secret not in str(exc.value)
    assert "[REDACTED" in str(exc.value)
    client.close()


def test_minimax_h3_redacts_a_bare_bearer_and_an_unprefixed_key():
    """An upstream may echo the token without the header name around it."""

    provider = MetasoMiniMaxVideoProvider("plainsecretvalue12345")

    redacted = provider._safe_error(
        "upstream said Bearer abc123def and also plainsecretvalue12345"
    )

    assert "abc123def" not in redacted
    assert "plainsecretvalue12345" not in redacted
    assert "[REDACTED" in redacted


def _video_task_handler(download):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/v2/video_generation"):
            return httpx.Response(200, json={"task_id": "task-1"})
        if "/v2/query/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "task": {
                        "status": "success",
                        "content": {"url": "https://cdn.example.test/video.mp4"},
                    }
                },
            )
        return download()

    return handler


def test_minimax_h3_applies_the_byte_ceiling_while_streaming():
    """A chunked body with no Content-Length must not be buffered whole first.

    The ceiling has to stop the transfer, not merely reject the finished
    buffer: the assertion is on how much of the body was ever produced.
    """

    consumed = 0

    def download():
        def chunks():
            nonlocal consumed
            for _ in range(8):
                consumed += 1
                yield b"\x00" * (1024 * 1024)

        return httpx.Response(
            200, content=chunks(), headers={"content-type": "video/mp4"}
        )

    client = httpx.Client(transport=httpx.MockTransport(_video_task_handler(download)))
    provider = MetasoMiniMaxVideoProvider(
        "test-key",
        base_url="https://metaso.example.test/api/minimax",
        client=client,
        sleep=lambda _seconds: None,
        max_bytes=2 * 1024 * 1024,
        url_validator=lambda _url: None,
    )

    with pytest.raises(RuntimeError, match="byte limit"):
        provider.generate(VideoGenerationRequest(prompt="test"))
    # 2 MiB ceiling over 1 MiB chunks stops on the third chunk; buffering the
    # whole body first would have produced all eight.
    assert consumed <= 3, f"body was buffered whole: {consumed} chunks read"
    client.close()


def test_minimax_h3_validates_every_redirect_target():
    """The guard is consulted on the redirect target, not only the first URL."""

    seen: list[str] = []

    def validator(url: str) -> None:
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("media URL resolved to a non-public address")

    def download():
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1:8001/internal"}
        )

    client = httpx.Client(transport=httpx.MockTransport(_video_task_handler(download)))
    provider = MetasoMiniMaxVideoProvider(
        "test-key",
        base_url="https://metaso.example.test/api/minimax",
        client=client,
        sleep=lambda _seconds: None,
        url_validator=validator,
    )

    with pytest.raises(RuntimeError, match="unsafe video URL"):
        provider.generate(VideoGenerationRequest(prompt="test"))
    assert seen == [
        "https://cdn.example.test/video.mp4",
        "http://127.0.0.1:8001/internal",
    ]
    client.close()
