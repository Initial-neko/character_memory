from __future__ import annotations

import io
from types import SimpleNamespace
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from character_memory.qwen3_voice_design_experiment import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoiceDesignRuntime,
    create_voice_design_app,
)


class FakeCuda:
    def __init__(self, *, available: bool = True, bf16: bool = True):
        self.available = available
        self.bf16 = bf16
        self.empty_cache_calls = 0
        self.synchronize_calls = 0
        self.reset_peak_calls = 0
        self.reset_peak_memory_stats = self._reset_peak
        self.empty_cache = self._empty_cache
        self.synchronize = self._synchronize

    def _empty_cache(self):
        self.empty_cache_calls += 1

    def _synchronize(self):
        self.synchronize_calls += 1

    def _reset_peak(self):
        self.reset_peak_calls += 1

    def is_available(self):
        return self.available

    def is_bf16_supported(self):
        return self.bf16

    def memory_allocated(self):
        return 100 * 1024 * 1024

    def memory_reserved(self):
        return 120 * 1024 * 1024

    def max_memory_allocated(self):
        return 140 * 1024 * 1024


class FakeTorch:
    """Enough of torch for device/dtype resolution and CUDA bookkeeping."""

    float32 = "torch.float32"
    bfloat16 = "torch.bfloat16"
    float16 = "torch.float16"

    def __init__(self, *, cuda_available: bool = True, bf16: bool = True):
        self.cuda = FakeCuda(available=cuda_available, bf16=bf16)


class FakeVoiceDesignModel:
    def __init__(self, *, kind: str = "voice_design", sample_rate: int = 24000, error=None):
        self.model = SimpleNamespace(tts_model_type=kind)
        self.calls: list[dict] = []
        self._sample_rate = sample_rate
        self._error = error

    def generate_voice_design(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        samples = np.asarray([0.0, 0.25, -0.25, 0.5], dtype=np.float32)
        return [samples], self._sample_rate


def make_runtime(model=None, *, torch=None, **kwargs) -> tuple[VoiceDesignRuntime, FakeVoiceDesignModel]:
    model = model or FakeVoiceDesignModel()
    runtime = VoiceDesignRuntime(
        model_loader=lambda **_kwargs: model,
        torch_module=torch or FakeTorch(),
        **kwargs,
    )
    return runtime, model


def read_wav(payload: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(payload), "rb") as wav:
        return wav.getnchannels(), wav.getsampwidth(), wav.getframerate()


# --- readiness contract -------------------------------------------------------


def test_health_returns_200_and_a_reason_when_not_ready(monkeypatch):
    """A non-2xx /health means "sidecar not running" to the TTS Lab adapter.

    So an unready VoiceDesign runtime must still answer 200 and explain itself in
    the body. Otherwise the Workbench cannot tell "not configured" apart from
    "not started", and shows the wrong remedy.
    """
    monkeypatch.setattr(
        VoiceDesignRuntime,
        "_dependency_status",
        lambda self: (False, "Missing isolated Qwen3-TTS dependencies: qwen_tts"),
    )
    runtime = VoiceDesignRuntime()
    with TestClient(create_voice_design_app(runtime)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["loaded"] is False
    assert "qwen_tts" in body["reason"]


def test_health_reports_lazy_readiness_before_the_model_is_loaded():
    """VoiceDesign 1.7B reserves ~4.4 GB, so it must load lazily on first use.

    Ready-but-not-loaded is therefore the normal resting state, and the
    Workbench shows it as usable without forcing a load.
    """
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        body = client.get("/health").json()

    assert body["ready"] is True
    assert body["loaded"] is False
    assert body["model"] == DEFAULT_MODEL
    assert body["reason"] is None


def test_status_advertises_the_model_and_language_it_will_use():
    runtime, _model = make_runtime(model_id="local/1.7B-VoiceDesign", language="Japanese")
    status = runtime.status()
    assert status["id"] == "qwen3-voice-design"
    assert status["model"] == "local/1.7B-VoiceDesign"
    assert status["language"] == "Japanese"
    assert status["loaded"] is False


# --- synthesis ----------------------------------------------------------------


def test_generate_passes_the_voice_design_contract_to_the_model():
    runtime, model = make_runtime()
    result = runtime.generate(
        VoiceDesignRequest(
            text="你好，这是试听。",
            instruct="年轻女性，清亮柔和",
            language="Chinese",
            max_new_tokens=1536,
        )
    )

    assert model.calls == [
        {
            "text": "你好，这是试听。",
            "instruct": "年轻女性，清亮柔和",
            "language": "Chinese",
            "max_new_tokens": 1536,
        }
    ]
    assert isinstance(result, VoiceDesignResult)
    assert result.sample_rate == 24000
    assert result.language == "Chinese"


def test_generate_loads_the_model_lazily(tmp_path):
    runtime, _model = make_runtime()
    assert runtime.status()["loaded"] is False

    runtime.generate(VoiceDesignRequest(text="你好", instruct="清亮"))

    assert runtime.status()["loaded"] is True


def test_generate_ignores_a_blank_language_and_falls_back_to_the_default():
    runtime, model = make_runtime()
    runtime.generate(VoiceDesignRequest(text="你好", instruct="清亮", language="   "))
    assert model.calls[0]["language"] == DEFAULT_LANGUAGE


def test_generate_rejects_text_that_is_only_whitespace():
    runtime, _model = make_runtime()
    with pytest.raises(ValueError, match="empty VoiceDesign text"):
        runtime.generate(VoiceDesignRequest(text="   ", instruct="清亮"))


def test_generate_wraps_model_failures_with_context():
    runtime, _model = make_runtime(model=FakeVoiceDesignModel(error=RuntimeError("CUDA out of memory")))
    with pytest.raises(RuntimeError, match="VoiceDesign generation failed: CUDA out of memory"):
        runtime.generate(VoiceDesignRequest(text="你好", instruct="清亮"))


# --- checkpoint guard ---------------------------------------------------------


def test_load_rejects_a_checkpoint_that_is_not_a_voice_design_model():
    """The model id comes from a hand-edited launcher env, so a wrong value must
    fail with the fix in the message rather than a deep shape error later."""
    runtime, _model = make_runtime(model=FakeVoiceDesignModel(kind="custom_voice"))
    with pytest.raises(RuntimeError) as excinfo:
        runtime.load()

    message = str(excinfo.value)
    assert "custom_voice" in message
    assert "voice_design" in message
    assert "QWEN3_VOICE_DESIGN_MODEL" in message
    assert runtime.status()["loaded"] is False


# --- unload -------------------------------------------------------------------


def test_unload_releases_the_model_and_returns_cuda_memory():
    torch = FakeTorch()
    runtime, _model = make_runtime(torch=torch)
    runtime.load()
    assert runtime.status()["loaded"] is True

    torch.cuda.empty_cache_calls = 0
    status = runtime.unload()

    assert status["loaded"] is False
    assert torch.cuda.empty_cache_calls >= 1


def test_unload_is_safe_when_nothing_is_loaded():
    runtime, _model = make_runtime()
    assert runtime.unload()["loaded"] is False


# --- HTTP surface -------------------------------------------------------------


def test_voice_design_endpoint_returns_wav_with_canonical_headers():
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        response = client.post(
            "/v1/voice-design",
            json={"text": "你好", "instruct": "清亮柔和", "language": "Chinese"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-voice-design-model"] == DEFAULT_MODEL
    assert response.headers["x-voice-design-device"] == "cuda:0"
    assert response.headers["x-voice-design-sample-rate"] == "24000"
    assert float(response.headers["x-voice-design-inference-ms"]) >= 0.0
    assert float(response.headers["x-voice-design-audio-ms"]) > 0.0

    channels, width, rate = read_wav(response.content)
    assert (channels, width, rate) == (1, 2, 24000)


def test_voice_design_endpoint_does_not_emit_legacy_tts_headers():
    """The adapter prefers the canonical names and only falls back to X-TTS-* for
    older peers. Sending both is redundant and hides which one was read."""
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        response = client.post("/v1/voice-design", json={"text": "你好", "instruct": "清亮"})

    assert not [name for name in response.headers if name.lower().startswith("x-tts-")]


@pytest.mark.parametrize("payload", [{}, {"text": ""}])
def test_voice_design_endpoint_rejects_an_empty_text(payload):
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        response = client.post("/v1/voice-design", json=payload)
    assert response.status_code == 422


def test_voice_design_endpoint_maps_model_failure_to_503():
    runtime, _model = make_runtime(model=FakeVoiceDesignModel(error=RuntimeError("CUDA out of memory")))
    with TestClient(create_voice_design_app(runtime)) as client:
        response = client.post("/v1/voice-design", json={"text": "你好", "instruct": "清亮"})

    assert response.status_code == 503
    assert "CUDA out of memory" in response.json()["detail"]


def test_load_and_unload_endpoints_are_available_for_vram_control():
    """GSV and VoiceDesign cannot be resident together on an 8 GB card, so the
    Workbench needs an explicit way to hand the card back before returning to
    live chat."""
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        loaded = client.post("/v1/load")
        assert loaded.status_code == 200
        assert loaded.json()["loaded"] is True

        unloaded = client.post("/v1/unload")
        assert unloaded.status_code == 200
        assert unloaded.json()["loaded"] is False


def test_there_is_no_configure_endpoint():
    """One model, and its id is a contract constant on both sides. Making it
    runtime-mutable would create a second source of truth the Workbench can
    neither see nor validate."""
    runtime, _model = make_runtime()
    with TestClient(create_voice_design_app(runtime)) as client:
        assert client.post("/v1/configure", json={}).status_code == 404
