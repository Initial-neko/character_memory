from __future__ import annotations

import io
import sys
from types import SimpleNamespace
import wave
from pathlib import Path

import numpy as np
import pytest

import character_memory.media_server as media_server
from character_memory.media_runtime import (
    MediaRuntime,
    SherpaSenseVoiceProvider,
    SherpaVitsProvider,
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
    def __init__(self, *, ready: bool = True):
        self.calls = []
        self.ready = ready

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
        return {
            "ready": self.ready,
            "loaded": self.ready,
            "provider": "fake-tts",
            "model": "fake",
            "device": "cpu",
            "reason": None if self.ready else "fake local tts unavailable",
        }


class _ProviderStatusResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _ProviderClient:
    def __init__(self):
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _ProviderStatusResponse(
            {
                "provider": {
                    "id": "kokoro",
                    "ready": True,
                    "loaded": False,
                    "model": "fake-kokoro",
                    "device": "cpu",
                    "reason": None,
                }
            }
        )

    def post(self, *args, **kwargs):
        raise AssertionError("health test must not synthesize")



class _QwenProviderResponse:
    def __init__(self, payload=None, *, status_code=200, content=b"", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = "" if payload is None else str(payload)

    @property
    def is_error(self):
        return self.status_code >= 400

    def raise_for_status(self):
        if self.is_error:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _QwenProviderClient:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _QwenProviderResponse(
            {
                "ok": True,
                "id": "qwen3",
                "ready": True,
                "loaded": False,
                "model": "fake-qwen3",
                "device": "cuda:0",
                "dtype": "float16",
                "reason": None,
            }
        )

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _QwenProviderResponse(
            content=b"RIFFfake",
            headers={
                "x-tts-voice": "Vivian",
                "x-tts-device": "cuda:0",
                "x-tts-inference-ms": "123.4",
                "x-tts-audio-ms": "1000",
                "x-tts-sample-rate": "24000",
                "x-tts-rtf": "0.1234",
            },
        )


class _EdgeProviderClient:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _QwenProviderResponse(
            {"provider": {"id": "edge", "ready": True, "loaded": True, "model": "Microsoft Edge Read Aloud", "device": "cloud", "network_required": True, "reason": None}}
        )

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _QwenProviderResponse(
            content=b"ID3edge-mp3",
            headers={
                "content-type": "audio/mpeg",
                "x-tts-voice": "zh-CN-XiaoxiaoNeural",
                "x-tts-device": "cloud",
                "x-tts-inference-ms": "155.5",
                "x-tts-audio-ms": "1200",
                "x-tts-sample-rate": "24000",
            },
        )


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

    for _ in range(8):
        runtime.transcribe_wav(make_wav(20))
        runtime.synthesize("继续")
    assert len(runtime.recent_metrics(200)) == 10


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
    assert health.json()["tts_runtime"]["provider"] == "fake-tts"
    assert health.json()["tts_selected"]["ready"] is True

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


def test_configured_kokoro_health_uses_selected_provider_not_local_sherpa(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(FakeAsr(), FakeTts(ready=False))
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(
        media_server,
        "load_settings",
        lambda _path: SimpleNamespace(
            tts_provider="kokoro",
            tts_voice="zf_001",
            tts_speed=1.0,
            tts_device="cpu",
        ),
    )
    provider_client = _ProviderClient()

    with TestClient(media_server.create_media_app(provider_http_client=provider_client)) as client:
        health = client.get("/health")

    assert health.status_code == 200
    payload = health.json()
    assert payload["tts_runtime"]["ready"] is False
    assert payload["tts_runtime"]["provider"] == "fake-tts"
    assert payload["tts"]["provider"] == "kokoro"
    assert payload["tts"]["ready"] is True
    assert payload["tts"]["model"] == "fake-kokoro"
    assert payload["tts_selected"] == payload["tts"]
    assert provider_client.get_calls == [
        ("http://127.0.0.1:9002/v1/providers/kokoro", {"timeout": 0.4})
    ]




def test_configured_edge_health_uses_provider_runtime(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(FakeAsr(), FakeTts(ready=False))
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(
        media_server,
        "load_settings",
        lambda _path: SimpleNamespace(tts_provider="edge", tts_voice="zh-CN-XiaoxiaoNeural", tts_speed=1.0, tts_device="cpu"),
    )
    provider_client = _EdgeProviderClient()
    with TestClient(media_server.create_media_app(provider_http_client=provider_client)) as client:
        health = client.get("/health")

    assert health.status_code == 200
    payload = health.json()
    assert payload["tts_runtime"]["ready"] is False
    assert payload["tts"]["provider"] == "edge"
    assert payload["tts"]["ready"] is True
    assert payload["tts"]["device"] == "cloud"
    assert provider_client.get_calls == [("http://127.0.0.1:9002/v1/providers/edge", {"timeout": 0.4})]


def test_configured_edge_tts_routes_mp3_through_media_runtime(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(FakeAsr(), FakeTts(ready=False))
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(
        media_server,
        "load_settings",
        lambda _path: SimpleNamespace(tts_provider="edge", tts_voice="zh-CN-XiaoxiaoNeural", tts_speed=1.0, tts_device="cpu"),
    )
    provider_client = _EdgeProviderClient()
    with TestClient(media_server.create_media_app(provider_http_client=provider_client)) as client:
        response = client.post("/v1/tts", json={"text": "你好"})

    assert response.status_code == 200
    assert response.content == b"ID3edge-mp3"
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["x-media-provider"] == "edge"
    assert response.headers["x-media-voice"] == "zh-CN-XiaoxiaoNeural"
    assert response.headers["x-media-device"] == "cloud"
    assert provider_client.post_calls == [
        ("http://127.0.0.1:9002/v1/tts", {"json": {"provider": "edge", "text": "你好", "voice": "zh-CN-XiaoxiaoNeural", "speed": 1.0}})
    ]


def test_configured_qwen3_health_uses_isolated_sidecar(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(FakeAsr(), FakeTts(ready=False))
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(
        media_server,
        "load_settings",
        lambda _path: SimpleNamespace(
            tts_provider="qwen3",
            tts_voice="Vivian",
            tts_speed=1.0,
            tts_device="cuda",
        ),
    )
    provider_client = _QwenProviderClient()

    with TestClient(media_server.create_media_app(provider_http_client=provider_client)) as client:
        health = client.get("/health")

    assert health.status_code == 200
    payload = health.json()
    assert payload["tts_runtime"]["ready"] is False
    assert payload["tts"]["provider"] == "qwen3"
    assert payload["tts"]["ready"] is True
    assert payload["tts"]["model"] == "fake-qwen3"
    assert payload["tts"]["device"] == "cuda:0"
    assert provider_client.get_calls == [
        ("http://127.0.0.1:9013/health", {"timeout": 0.4})
    ]


def test_configured_qwen3_tts_routes_through_media_runtime(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runtime = MediaRuntime(FakeAsr(), FakeTts(ready=False))
    monkeypatch.setattr(media_server, "build_media_runtime_from_env", lambda: runtime)
    monkeypatch.setattr(
        media_server,
        "load_settings",
        lambda _path: SimpleNamespace(
            tts_provider="qwen3",
            tts_voice="Vivian",
            tts_speed=1.0,
            tts_device="cuda",
        ),
    )
    provider_client = _QwenProviderClient()

    with TestClient(media_server.create_media_app(provider_http_client=provider_client)) as client:
        response = client.post("/v1/tts", json={"text": "你好"})

    assert response.status_code == 200
    assert response.content == b"RIFFfake"
    assert response.headers["x-media-provider"] == "qwen3"
    assert response.headers["x-media-voice"] == "Vivian"
    assert response.headers["x-media-device"] == "cuda:0"
    assert response.headers["x-media-rtf"] == "0.1234"
    assert provider_client.post_calls == [
        (
            "http://127.0.0.1:9013/v1/tts",
            {
                "json": {
                    "text": "你好",
                    "voice": "Vivian",
                    "language": "Chinese",
                    "speed": 1.0,
                }
            },
        )
    ]

def test_asr_endpoint_rejects_wrong_media_type():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    client = TestClient(create_media_app(MediaRuntime(FakeAsr(), FakeTts())))
    response = client.post("/v1/asr", content=b"not wav", headers={"Content-Type": "audio/webm"})
    assert response.status_code == 415


def test_sensevoice_adapter_is_lazy_and_maps_sherpa_contract(monkeypatch, tmp_path):
    model = tmp_path / "model.int8.onnx"
    tokens = tmp_path / "tokens.txt"
    model.write_bytes(b"model")
    tokens.write_text("tokens", encoding="utf-8")
    captured = {}

    class FakeStream:
        def __init__(self):
            self.result = SimpleNamespace(text="  你好，世界  ")

        def accept_waveform(self, sample_rate, samples):
            captured["sample_rate"] = sample_rate
            captured["sample_count"] = len(samples)

    class FakeRecognizer:
        def create_stream(self):
            return FakeStream()

        def decode_stream(self, stream):
            captured["decoded"] = True

    class FakeOfflineRecognizer:
        @classmethod
        def from_sense_voice(cls, **kwargs):
            captured["asr_config"] = kwargs
            return FakeRecognizer()

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(OfflineRecognizer=FakeOfflineRecognizer))
    provider = SherpaSenseVoiceProvider(
        model=str(model),
        tokens=str(tokens),
        device="cpu",
        language="zh",
        num_threads=3,
    )
    assert provider.status()["loaded"] is False
    assert "asr_config" not in captured

    result = provider.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    assert result.text == "你好，世界"
    assert result.provider == "sherpa-sensevoice"
    assert provider.status()["loaded"] is True
    assert captured["asr_config"]["model"] == str(model)
    assert captured["asr_config"]["tokens"] == str(tokens)
    assert captured["asr_config"]["provider"] == "cpu"
    assert captured["asr_config"]["language"] == "zh"
    assert captured["asr_config"]["num_threads"] == 3
    assert captured["sample_rate"] == 16000
    assert captured["sample_count"] == 1600
    assert captured["decoded"] is True


def test_vits_adapter_is_lazy_and_maps_sherpa_contract(monkeypatch, tmp_path):
    model = tmp_path / "model.onnx"
    tokens = tmp_path / "tokens.txt"
    lexicon = tmp_path / "lexicon.txt"
    for item in (model, tokens, lexicon):
        item.write_bytes(b"x")
    captured = {}

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            captured.setdefault("configs", []).append((type(self).__name__, kwargs))

    class FakeVitsConfig(Config):
        pass

    class FakeModelConfig(Config):
        pass

    class FakeTtsConfig(Config):
        pass

    class FakeOfflineTts:
        def __init__(self, config):
            captured["tts_config"] = config

        def generate(self, text, sid=0, speed=1.0):
            captured["generate"] = (text, sid, speed)
            return SimpleNamespace(samples=np.zeros(800, dtype=np.float32), sample_rate=16000)

    fake_module = SimpleNamespace(
        OfflineTtsVitsModelConfig=FakeVitsConfig,
        OfflineTtsModelConfig=FakeModelConfig,
        OfflineTtsConfig=FakeTtsConfig,
        OfflineTts=FakeOfflineTts,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)
    provider = SherpaVitsProvider(
        model=str(model),
        tokens=str(tokens),
        lexicon=str(lexicon),
        device="cpu",
        num_threads=4,
    )
    assert provider.status()["loaded"] is False
    assert "tts_config" not in captured

    result = provider.synthesize("测试 TTS", speaker_id=2, speed=1.1)
    assert result.audio.startswith(b"RIFF")
    assert result.sample_rate == 16000
    assert result.provider == "sherpa-vits"
    assert provider.status()["loaded"] is True
    assert captured["generate"] == ("测试 TTS", 2, 1.1)
    configs = captured["configs"]
    assert configs[0][1]["model"] == str(model)
    assert configs[0][1]["tokens"] == str(tokens)
    assert configs[0][1]["lexicon"] == str(lexicon)
    assert configs[1][1]["provider"] == "cpu"
    assert configs[1][1]["num_threads"] == 4


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
