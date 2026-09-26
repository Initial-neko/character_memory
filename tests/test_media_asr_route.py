import pytest

from character_memory.media_runtime import MediaRuntime, SpeechRecognitionProvider, TextToSpeechProvider
from character_memory.media_server import create_media_app


class _UnusedAsr(SpeechRecognitionProvider):
    def transcribe(self, samples, sample_rate):  # pragma: no cover - route schema test only
        raise AssertionError("not called")

    def status(self):
        return {"ready": True, "provider": "unused"}


class _UnusedTts(TextToSpeechProvider):
    def synthesize(self, text, *, speaker_id=0, speed=1.0):  # pragma: no cover - route schema test only
        raise AssertionError("not called")

    def status(self):
        return {"ready": True, "provider": "unused"}


def test_asr_openapi_uses_raw_body_and_content_type_header_not_fake_request_parameter():
    pytest.importorskip("fastapi")
    app = create_media_app(MediaRuntime(_UnusedAsr(), _UnusedTts()))
    operation = app.openapi()["paths"]["/v1/asr"]["post"]
    params = operation.get("parameters", [])
    assert not any(item.get("name") == "request" for item in params)
    assert any(item.get("name") == "Content-Type" and item.get("in") == "header" for item in params)
    assert "requestBody" in operation


class _FakeStreamResult:
    def __init__(self, text):
        self.text = text


class _FakeStreamingSession:
    def __init__(self):
        self.calls = 0
        self.closed = False

    def push_audio(self, samples, *, sample_rate):
        self.calls += 1
        assert sample_rate == 16000
        assert samples.dtype.name == "float32"
        return "正在识别你好" if self.calls == 1 else "正在识别你好世界"

    def is_endpoint(self):
        return self.calls >= 2

    def finish(self):
        self.closed = True
        return _FakeStreamResult("你好世界")


class _FakeStreamingAsr(SpeechRecognitionProvider):
    def __init__(self):
        self.sessions = []

    def transcribe(self, samples, sample_rate):  # pragma: no cover
        raise AssertionError("streaming test must not call batch transcribe")

    def status(self):
        return {"ready": True, "provider": "fake-stream"}

    def create_session(self):
        session = _FakeStreamingSession()
        self.sessions.append(session)
        return session


def test_streaming_asr_websocket_emits_partial_then_one_final():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(_FakeStreamingAsr(), _UnusedTts())
    with TestClient(create_media_app(runtime)).websocket_connect("/v1/asr/stream") as ws:
        ws.send_json({"op": "start", "source": "call"})
        ready = ws.receive_json()
        assert ready["kind"] == "ready"

        ws.send_bytes(b"\\x00\\x00" * 160)
        partial = ws.receive_json()
        assert partial["kind"] == "partial"
        assert partial["segment_id"] == 1

        ws.send_bytes(b"\\x00\\x00" * 160)
        final = ws.receive_json()
        assert final["kind"] == "final"
        assert final["text"] == "你好世界"
        assert final["segment_id"] == 1
        assert final["endpoint_reason"] == "model_endpoint"
