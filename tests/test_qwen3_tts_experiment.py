from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
from fastapi.testclient import TestClient

from character_memory.qwen3_tts_experiment import (
    Qwen3TtsRequest,
    Qwen3TtsResult,
    create_qwen3_tts_app,
    float_audio_to_wav,
)


class FakeQwen3Runtime:
    preload = False

    def __init__(self):
        self.loaded = False

    def status(self) -> dict:
        return {
            "id": "qwen3",
            "ready": True,
            "loaded": self.loaded,
            "mode": "custom",
            "model": "fake-qwen3",
            "device": "cuda:0",
            "dtype": "float16",
            "attn_implementation": "sdpa",
            "voices": ["Vivian"],
            "default_voice": "Vivian",
            "supports_speed": False,
            "load_ms": 123.4 if self.loaded else None,
            "load_cuda_peak_mb": 2500.0 if self.loaded else None,
            "cuda_allocated_mb": 2100.0 if self.loaded else None,
            "cuda_reserved_mb": 2300.0 if self.loaded else None,
            "reason": None,
        }

    def load(self) -> dict:
        already = self.loaded
        self.loaded = True
        return {**self.status(), "already_loaded": already}

    def synthesize(self, request: Qwen3TtsRequest) -> Qwen3TtsResult:
        assert request.text == "你好"
        assert request.voice == "Vivian"
        self.loaded = True
        return Qwen3TtsResult(
            audio=b"RIFFfake-qwen3-wav",
            sample_rate=24000,
            model="fake-qwen3",
            device="cuda:0",
            dtype="float16",
            attn_implementation="sdpa",
            voice="Vivian",
            mode="custom",
            inference_ms=88.8,
            audio_ms=500.0,
            rtf=0.1776,
            cuda_allocated_mb=2100.0,
            cuda_reserved_mb=2300.0,
            cuda_peak_mb=2600.0,
        )


def test_qwen3_sidecar_http_contract_without_loading_real_model():
    runtime = FakeQwen3Runtime()
    app = create_qwen3_tts_app(runtime)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["loaded"] is False

        loaded = client.post("/v1/load")
        assert loaded.status_code == 200
        assert loaded.json()["loaded"] is True
        assert loaded.json()["already_loaded"] is False

        response = client.post(
            "/v1/tts",
            json={"text": "你好", "voice": "Vivian", "language": "Chinese", "speed": 1.0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["x-tts-provider"] == "qwen3"
        assert response.headers["x-tts-model"] == "fake-qwen3"
        assert response.headers["x-tts-device"] == "cuda:0"
        assert response.headers["x-tts-inference-ms"] == "88.8"
        assert response.headers["x-tts-rtf"] == "0.1776"
        assert response.headers["x-tts-cuda-peak-mb"] == "2600.0"
        assert response.content == b"RIFFfake-qwen3-wav"



def test_qwen3_auto_dtype_prefers_bfloat16_on_supported_cuda():
    runtime = __import__("character_memory.qwen3_tts_experiment", fromlist=["Qwen3TtsRuntime"]).Qwen3TtsRuntime()
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_bf16_supported=lambda: True),
        bfloat16=object(),
        float16=object(),
        float32=object(),
    )
    name, _dtype = runtime._resolve_dtype(fake_torch, "cuda:0")
    assert name == "bfloat16"


def test_qwen3_auto_dtype_avoids_float16_when_bfloat16_is_unavailable():
    runtime = __import__("character_memory.qwen3_tts_experiment", fromlist=["Qwen3TtsRuntime"]).Qwen3TtsRuntime()
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_bf16_supported=lambda: False),
        bfloat16=object(),
        float16=object(),
        float32=object(),
    )
    name, _dtype = runtime._resolve_dtype(fake_torch, "cuda:0")
    assert name == "float32"

def test_qwen3_float_audio_encoder_returns_pcm16_mono_wav():
    payload = float_audio_to_wav(np.asarray([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float32), 24000)
    with wave.open(io.BytesIO(payload), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 5


def test_qwen3_experiment_stays_isolated_from_core_environment():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8").lower()
    requirements = Path("scripts/qwen3-tts-requirements.txt").read_text(encoding="utf-8")
    setup = Path("scripts/setup-qwen3-tts.sh").read_text(encoding="utf-8")
    start = Path("scripts/start-qwen3-tts.sh").read_text(encoding="utf-8")
    benchmark = Path("scripts/benchmark_qwen3_tts.py").read_text(encoding="utf-8")
    module = Path("src/character_memory/qwen3_tts_experiment.py").read_text(encoding="utf-8")

    assert "qwen-tts" not in pyproject
    assert "qwen-tts==0.1.1" in requirements
    assert ".venv-qwen3-tts" in setup
    assert 'uv venv "$VENV" --python 3.12' in setup
    assert "prefetch_qwen3_tts.py" in setup
    assert "character_memory.qwen3_tts_experiment" in start
    assert "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" in module
    assert "Qwen3TTSModel.from_pretrained" in module
    assert "attn_implementation" in module
    assert "generate_custom_voice" in module
    assert "generate_voice_clone" in module
    assert "p50" in benchmark and "p95" in benchmark
    assert "cuda_peak_mb" in benchmark
    assert "benchmark.json" in benchmark
