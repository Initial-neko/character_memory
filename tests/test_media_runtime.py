from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from character_memory.media_runtime import (
    MediaRuntime,
    SpeechRecognitionProvider,
    SynthesisResult,
    TextToSpeechProvider,
    TranscriptionResult,
    float_audio_to_wav,
    read_pcm16_wav,
    resample_linear,
)
from character_memory.media_server import create_media_app


class FakeAsr(SpeechRecognitionProvider):
    def __init__(self):
        self.calls = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        self.calls += 1
        assert samples.dtype == np.float32
        assert sample_rate == 16000
        return TranscriptionResult(
            text="测试语音",
            provider="fake-asr",
            model="fake",
            device="cpu",
            inference_ms=12.5,
            audio_ms=len(samples) * 1000.0 / sample_rate,
        )

    def status(self) -> dict:
        return {"ready": True, "loaded": True, "provider": "fake-asr", "device": "cpu"}


class FakeTts(TextToSpeechProvider):
    def __init__(self):
        self.calls = []

    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        self.calls.append((text, speaker_id, speed))
        samples = np.zeros(1600, dtype=np.float32)
        return SynthesisResult(
            audio=float_audio_to_wav(samples, 16000),
            sample_rate=16000,
            provider="fake-tts",
            model="fake",
            device="cpu",
            inference_ms=8.0,
            audio_ms=100.0,
        )

    def status(self) -> dict:
        return {"ready": True, "loaded": True, "provider": "fake-tts", "device": "cpu"}


def make_wav(duration_ms: int = 200, sample_rate: int = 16000) -> bytes:
    count = int(sample_rate * duration_ms / 1000)
    t = np.arange(count, dtype=np.float32) / sample_rate
    samples = 0.1 * np.sin(2 * np.pi * 440 * t)
    return float_audio_to_wav(samples, sample_rate)


def test_pcm16_wav_roundtrip_contract():
    payload = make_wav(100)
    samples, sample_rate = read_pcm16_wav(payload)
    assert sample_rate == 16000
    assert samples.dtype == np.float32
    assert len(samples) == 1600
    assert float(np.max(np.abs(samples))) <= 0.101


def test_read_wav_rejects_non_pcm16():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(16000)
        wav.writeframes(b"\x80" * 100)
    with pytest.raises(ValueError, match="16-bit PCM"):
        read_pcm16_wav(out.getvalue())


def test_linear_resample_has_expected_length():
    source = np.linspace(-1, 1, 4800, dtype=np.float32)
    result = resample_linear(source, 48000, 16000)
    assert result.dtype == np.float32
    assert len(result) == 1600


def test_runtime_records_bounded_asr_and_tts_metrics():
    runtime = MediaRuntime(FakeAsr(), FakeTts(), metrics_limit=10)
    result = runtime.transcribe_wav(make_wav())
    assert result.text == "测试语音"
    speech = runtime.synthesize("你好", speaker_id=2, speed=1.1)
    assert speech.audio.startswith(b"RIFF")
    metrics = runtime.recent_metrics()
    assert [item["kind"] for item in metrics] == ["asr", "tts"]
    assert metrics[0]["inference_ms"] == 12.5
    assert metrics[1]["inference_ms"] == 8.0


def test_media_http_contract_with_fake_providers():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    asr = FakeAsr()
    tts = FakeTts()
    runtime = MediaRuntime(asr, tts)
    client = TestClient(create_media_app(runtime))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["asr"]["provider"] == "fake-asr"
    assert health.json()["tts"]["provider"] == "fake-tts"

    asr_response = client.post("/v1/asr", content=make_wav(), headers={"Content-Type": "audio/wav"})
    assert asr_response.status_code == 200
    assert asr_response.json()["text"] == "测试语音"
    assert asr.calls == 1

    tts_response = client.post("/v1/tts", json={"text": "你好", "speaker_id": 3, "speed": 1.05})
    assert tts_response.status_code == 200
    assert tts_response.headers["content-type"].startswith("audio/wav")
    assert tts_response.content.startswith(b"RIFF")
    assert tts_response.headers["x-media-provider"] == "fake-tts"
    assert tts.calls == [("你好", 3, 1.05)]

    metrics = client.get("/v1/metrics/recent").json()["metrics"]
    assert [item["kind"] for item in metrics] == ["asr", "tts"]


def test_asr_endpoint_rejects_wrong_media_type():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    client = TestClient(create_media_app(MediaRuntime(FakeAsr(), FakeTts())))
    response = client.post("/v1/asr", content=b"not wav", headers={"Content-Type": "audio/webm"})
    assert response.status_code == 415


def test_media_process_does_not_import_character_runtime():
    source = Path("src/character_memory/media_server.py").read_text(encoding="utf-8")
    assert "character_memory.api" not in source
    assert "build_app" not in source
    assert "PersonRuntime" not in source


def test_main_server_does_not_import_media_runtime():
    source = Path("src/character_memory/server.py").read_text(encoding="utf-8")
    assert "media_server" not in source
    assert "media_runtime" not in source


def test_media_provider_model_dependencies_are_lazy():
    source = Path("src/character_memory/media_runtime.py").read_text(encoding="utf-8")
    # CI and normal text-chat installs must not import sherpa-onnx at module import time.
    top_level_prefix = source.split("class SherpaSenseVoiceProvider", 1)[0]
    assert "import sherpa_onnx" not in top_level_prefix
