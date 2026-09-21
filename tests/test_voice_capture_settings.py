"""Voice-call endpointing settings.

``voice.js`` decides the user has finished speaking by watching for a run of
silence in the microphone stream. That threshold used to be a hardcoded 450 ms,
which is shorter than an ordinary pause between clauses, so one sentence
arrived as two chat turns: the client shipped the partial audio to ``/v1/asr``
and dispatched it as a complete turn while the user was still talking.

Nothing was truncated -- silent frames are part of ``voice.chunks`` too -- which
is why recognition quality looked fine and only the *boundary* was wrong.

The threshold is now a setting (900 ms by default) and reaches the browser
through the Media Runtime ``/health`` payload that ``checkMedia()`` already
fetches before opening the microphone. Reusing that response keeps the change
free of an extra round trip and of a second source of truth.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import character_memory.media_server as media_server
from character_memory.config import Settings
from character_memory.media_runtime import (
    MediaRuntime,
    SpeechRecognitionProvider,
    TextToSpeechProvider,
    TranscriptionResult,
    SynthesisResult,
)
from character_memory.settings_store import SETTINGS_SCHEMA


ROOT = Path(__file__).resolve().parents[1]
VOICE_JS = ROOT / "src" / "character_memory" / "web" / "voice.js"


class _FakeAsr(SpeechRecognitionProvider):
    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        return TranscriptionResult(
            text="测试语音",
            provider="fake-asr",
            model="fake",
            device="cpu",
            inference_ms=1.0,
            audio_ms=100.0,
        )

    def status(self) -> dict:
        return {"ready": True, "loaded": True, "provider": "fake-asr", "device": "cpu"}


class _FakeTts(TextToSpeechProvider):
    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        return SynthesisResult(
            audio=np.zeros(160, dtype=np.float32),
            sample_rate=16000,
            provider="fake-tts",
            model="fake",
            device="cpu",
            inference_ms=1.0,
        )

    def status(self) -> dict:
        return {"ready": True, "loaded": True, "provider": "fake-tts", "device": "cpu"}


def _health_payload(monkeypatch, settings) -> dict:
    """Fetch /health with an injected runtime and a stubbed settings object."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(_FakeAsr(), _FakeTts())
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(media_server, "load_settings", lambda _path: settings)

    with TestClient(media_server.create_media_app(runtime=runtime)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    return response.json()


def test_voice_silence_ms_defaults_to_900():
    """450 ms cut people off mid-sentence; 900 tolerates a thinking pause."""

    assert Settings().voice_silence_ms == 900


@pytest.mark.parametrize("value", [199, 3001, 0, -1])
def test_voice_silence_ms_rejects_out_of_range_values(value):
    """A silent threshold of 0 would end every utterance instantly."""

    with pytest.raises(Exception):
        Settings(voice_silence_ms=value)


def test_settings_schema_exposes_voice_silence_ms_as_a_number():
    """Settings Center has to render it, or the setting is unreachable."""

    voice_section = next(section for section in SETTINGS_SCHEMA if section.get("id") == "voice")
    field = next(
        (item for item in voice_section["fields"] if item["name"] == "voice_silence_ms"),
        None,
    )

    assert field is not None, "voice_silence_ms must appear in the Settings Center schema"
    assert field["type"] == "number"
    assert field["min"] == 200
    assert field["max"] == 3000


def test_media_health_reports_the_configured_silence_threshold(monkeypatch):
    payload = _health_payload(
        monkeypatch,
        SimpleNamespace(
            tts_provider="sherpa",
            tts_voice="zf_001",
            tts_speed=1.0,
            tts_device="cpu",
            voice_silence_ms=1200,
        ),
    )

    assert payload["voice_capture"]["silence_ms"] == 1200


def test_media_health_falls_back_when_settings_lack_the_field(monkeypatch):
    """Existing callers stub ``load_settings`` with a bare SimpleNamespace.

    Reading the new field with a plain attribute access would turn every one of
    those stubs into an AttributeError, so the route must tolerate its absence.
    """

    payload = _health_payload(
        monkeypatch,
        SimpleNamespace(tts_provider="sherpa", tts_voice="zf_001", tts_speed=1.0, tts_device="cpu"),
    )

    assert payload["voice_capture"]["silence_ms"] == 900


def test_voice_js_reads_the_threshold_from_health_and_keeps_a_default():
    """The browser must use the served value, not the old hardcoded 450.

    This is a wiring check, not a behaviour test: voice.js is loaded as a plain
    browser script with no module system, so there is nothing to import it into
    a test. It pins the three things that can silently break -- that the value
    is read from the health payload, that reading it is actually wired into the
    call startup path, and that a default remains for an older runtime.
    """

    source = VOICE_JS.read_text(encoding="utf-8")

    assert "voice_capture" in source
    assert "function applyVoiceCapture(health)" in source
    assert "applyVoiceCapture(await checkMedia())" in source
    assert "DEFAULT_SILENCE_MS = 900" in source
    # The old hardcoded value is what cut people off mid-sentence.
    assert "silenceMs: 450" not in source
