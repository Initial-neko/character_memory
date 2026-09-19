from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
from fastapi.testclient import TestClient

from character_memory.gsv_tts_experiment import (
    GsvRuntimeConfigRequest,
    GsvTtsRequest,
    GsvTtsResult,
    GsvTtsRuntime,
    create_gsv_tts_app,
)


class FakeGsvRuntime:
    preload = False

    def __init__(self):
        self.loaded = False

    def status(self) -> dict:
        return {
            "id": "gsv",
            "ready": True,
            "loaded": self.loaded,
            "model": "fake-gpt.ckpt+fake-sovits.pth",
            "device": "cuda:0",
            "voices": ["murasame"],
            "default_voice": "murasame",
            "supports_speed": True,
            "sample_rate": 32000,
            "reason": None,
        }

    def load(self) -> dict:
        already = self.loaded
        self.loaded = True
        return {**self.status(), "already_loaded": already}

    def configure(self, request: GsvRuntimeConfigRequest) -> dict:
        self.loaded = bool(request.preload)
        return {**self.status(), "changed": ["gpt_model"]}

    def unload(self) -> dict:
        self.loaded = False
        return {**self.status(), "unloaded": True}

    def synthesize(self, request: GsvTtsRequest) -> GsvTtsResult:
        assert request.text == "你好"
        assert request.voice == "murasame"
        assert request.language == "zh"
        assert request.speed == 1.0
        self.loaded = True
        return GsvTtsResult(
            audio=b"RIFFfake-gsv-wav",
            sample_rate=32000,
            model="fake-gpt.ckpt+fake-sovits.pth",
            device="cuda:0",
            voice="murasame",
            inference_ms=612.4,
            audio_ms=2886.0,
            rtf=0.2122,
            cuda_allocated_mb=2100.0,
            cuda_reserved_mb=2300.0,
            cuda_peak_mb=2600.0,
        )


def test_gsv_sidecar_http_contract_without_real_cuda():
    runtime = FakeGsvRuntime()
    app = create_gsv_tts_app(runtime)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["loaded"] is False

        configured = client.post(
            "/v1/configure",
            json={
                "gpt_model": "new.ckpt",
                "sovits_model": "new.pth",
                "ref_audio": "new.wav",
                "ref_text": "参考文本",
                "voice": "murasame",
                "device": "cuda",
                "preload": False,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["loaded"] is False

        loaded = client.post("/v1/load")
        assert loaded.status_code == 200
        assert loaded.json()["loaded"] is True
        assert loaded.json()["already_loaded"] is False

        response = client.post(
            "/v1/tts",
            json={"text": "你好", "voice": "murasame", "language": "zh", "speed": 1.0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["x-tts-provider"] == "gsv"
        assert response.headers["x-tts-voice"] == "murasame"
        assert response.headers["x-tts-device"] == "cuda:0"
        assert response.headers["x-tts-inference-ms"] == "612.4"
        assert response.headers["x-tts-rtf"] == "0.2122"
        assert response.headers["x-tts-cuda-peak-mb"] == "2600.0"
        assert response.content == b"RIFFfake-gsv-wav"


def test_gsv_runtime_uses_upstream_infer_batched_contract(tmp_path):
    gpt = tmp_path / "voice-e15.ckpt"
    sovits = tmp_path / "voice-e8.pth"
    ref = tmp_path / "reference.wav"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    ref.write_bytes(b"wav")

    calls = {}

    class FakeTts:
        def __init__(self, **kwargs):
            calls["init"] = kwargs
            self.tts_config = SimpleNamespace(device="cuda:0")

        def load_gpt_model(self, path):
            calls["gpt"] = path

        def load_sovits_model(self, path):
            calls["sovits"] = path

        def cache_spk_audio(self, path, **kwargs):
            calls["cache_spk"] = (path, kwargs)

        def cache_prompt_audio(self, **kwargs):
            calls["cache_prompt"] = kwargs

        def infer_batched(self, **kwargs):
            calls["infer"] = kwargs
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25, -0.25, 0.5], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cuda:0",
        default_voice="murasame",
        tts_factory=FakeTts,
    )

    status = runtime.status()
    assert status["ready"] is True
    assert status["loaded"] is False

    result = runtime.synthesize(
        GsvTtsRequest(text="你好", voice="murasame", language="zh", speed=1.2)
    )

    assert calls["init"]["gpt_cache"] == [(1, 512), (1, 1024), (4, 512), (4, 1024)]
    assert calls["init"]["sovits_cache"] == [50, 55]
    assert calls["init"]["use_bert"] is True
    assert calls["init"]["use_flash_attn"] is False
    assert calls["gpt"] == str(gpt)
    assert calls["sovits"] == str(sovits)
    assert calls["cache_spk"][0] == str(ref)
    assert calls["cache_prompt"]["prompt_audio_paths"] == str(ref)
    assert calls["infer"]["spk_audio_paths"] == str(ref)
    assert calls["infer"]["prompt_audio_paths"] == str(ref)
    assert calls["infer"]["prompt_audio_texts"] == "参考文本。"
    assert calls["infer"]["texts"] == "你好"
    assert calls["infer"]["text_languages"] == "zh"
    assert calls["infer"]["speed"] == 1.2
    assert calls["infer"]["gpt_model"] == str(gpt)
    assert calls["infer"]["sovits_model"] == str(sovits)

    assert result.sample_rate == 32000
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 32000
        assert wav.getnframes() == 4


def test_gsv_runtime_can_hot_configure_and_unload(tmp_path):
    gpt = tmp_path / "voice.ckpt"
    sovits = tmp_path / "voice.pth"
    ref = tmp_path / "ref.wav"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    ref.write_bytes(b"wav")

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cuda:0")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

    runtime = GsvTtsRuntime(
        gpt_model="",
        sovits_model="",
        ref_audio="",
        ref_text="",
        device="cuda",
        default_voice="murasame",
        tts_factory=FakeTts,
    )
    assert runtime.status()["ready"] is False

    configured = runtime.configure(
        GsvRuntimeConfigRequest(
            gpt_model=str(gpt),
            sovits_model=str(sovits),
            ref_audio=str(ref),
            ref_text="参考文本",
            voice="murasame",
            device="cuda",
            preload=True,
        )
    )
    assert configured["ready"] is True
    assert configured["loaded"] is True
    assert Path(runtime.gpt_model) == gpt
    assert runtime.ref_text == "参考文本"

    unloaded = runtime.unload()
    assert unloaded["ready"] is True
    assert unloaded["loaded"] is False


def test_gsv_status_reports_missing_assets_without_importing_gsv():
    runtime = GsvTtsRuntime(
        gpt_model="",
        sovits_model="",
        ref_audio="",
        ref_text="",
        tts_factory=lambda **kwargs: None,
    )
    status = runtime.status()
    assert status["ready"] is False
    assert "Missing GSV configuration" in status["reason"]
