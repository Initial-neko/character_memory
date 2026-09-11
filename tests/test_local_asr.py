from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from character_memory.speech import FunASRProvider


class _FakeTorch:
    class cuda:
        @staticmethod
        def is_available():
            return True


class _FakeAutoModel:
    created = []
    calls = []

    def __init__(self, **kwargs):
        self.created.append(kwargs)

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        assert Path(kwargs["input"]).is_file()
        return [{"text": "今天想和真由理聊一会儿"}]


def test_funasr_provider_is_lazy_and_passes_hotwords(monkeypatch):
    _FakeAutoModel.created.clear()
    _FakeAutoModel.calls.clear()
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=_FakeAutoModel))

    provider = FunASRProvider(model="test/fun-asr", device="cuda:0", hub="ms")
    assert _FakeAutoModel.created == []

    result = provider.transcribe(
        b"fake-webm",
        media_type="audio/webm;codecs=opus",
        language="中文",
        hotwords=["真由理", "菲利斯"],
    )

    assert result.text == "今天想和真由理聊一会儿"
    assert result.provider == "funasr"
    assert _FakeAutoModel.created[0]["device"] == "cuda:0"
    assert _FakeAutoModel.calls[0]["hotwords"] == ["真由理", "菲利斯"]
    assert _FakeAutoModel.calls[0]["language"] == "中文"


def test_speech_route_returns_draft_text_without_chat_submission(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from character_memory import speech_web
    from character_memory.speech import TranscriptionResult

    calls = []

    class FakeProvider:
        def transcribe(self, audio, *, media_type, language, hotwords=()):
            calls.append((audio, media_type, language, list(hotwords)))
            return TranscriptionResult(
                text="语音草稿",
                provider="fake",
                model="fake-model",
                device="cuda:0",
                transcription_ms=12.3,
            )

    monkeypatch.setattr(speech_web, "create_speech_provider", lambda settings: FakeProvider())
    app = FastAPI()
    app.state.character_memory = SimpleNamespace(
        settings=SimpleNamespace(
            asr_provider="funasr",
            asr_model="fake-model",
            asr_device="cuda:0",
            asr_hub="ms",
            asr_language="中文",
            asr_hotwords=["Character Memory"],
            asr_max_bytes=1024,
        ),
        character_profiles=lambda: [
            {"id": "rin", "name": "Rin"},
            {"id": "mayuri", "name": "真由理"},
        ],
    )
    speech_web.attach_speech_routes(app)

    client = TestClient(app)
    response = client.post(
        "/v1/speech/transcribe?character_id=mayuri",
        content=b"audio",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "语音草稿"
    assert calls[0][0] == b"audio"
    assert calls[0][2] == "中文"
    assert calls[0][3][:2] == ["真由理", "Rin"]


def test_web_speech_module_only_writes_draft():
    web = Path(__file__).parents[1] / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    speech = (web / "speech.js").read_text(encoding="utf-8")

    assert 'id="speechButton"' in index
    assert '/static/speech.js' in index
    assert '/v1/speech/transcribe' in speech
    assert "insertTranscript" in speech
    assert "requestSubmit" not in speech
    assert "submitCurrentText" not in speech
