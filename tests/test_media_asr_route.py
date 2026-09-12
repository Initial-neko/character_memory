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
