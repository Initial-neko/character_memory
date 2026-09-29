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
