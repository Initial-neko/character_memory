"""Qwen3-TTS 1.7B VoiceDesign tool sidecar (:9015).

This is a *tool*, not a realtime TTS provider. Two hard constraints put it
outside the live chat path:

- **VRAM.** The 1.7B checkpoint reserves ~4.4 GB. On an 8 GB card that leaves
  less than GSV needs (~1.4 GB), so the two can never be resident together.
  The model therefore loads lazily on first use and ``/v1/unload`` exists so the
  Workbench can hand the card back before returning to live chat.
- **Latency.** RTF is ~2.9-3.5 in steady state, and 13.6 on the first call after a
  load (cuDNN kernel autotune). Slower than realtime either way.

So it must never appear in ``config.yaml: tts_provider``, the Settings realtime
provider selector, Media Runtime routing, or the realtime A/B list; and
``character-stack`` must never preload or auto-start it.

There is deliberately **no** ``/v1/configure``. There is one model, and its id is
a contract constant on both sides (``tts_lab.Qwen3VoiceDesignSidecar.MODEL``).
Making it runtime-mutable would create a second source of truth that the
Workbench can neither see nor validate. Model choice belongs to the launcher
environment (``QWEN3_VOICE_DESIGN_MODEL``).

Heavy imports stay inside this process so Character Memory's core environment
stays independent of the Torch/CUDA ABI this model needs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
import importlib.util
import os
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import numpy as np
from pydantic import BaseModel, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.qwen3_tts_experiment import float_audio_to_wav


DEFAULT_PORT = 9015
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
DEFAULT_LANGUAGE = "Chinese"
DEFAULT_MAX_NEW_TOKENS = 2048

#: Values ``tts_model_type`` takes in the qwen_tts config. Only the first is
#: usable here; the others are named in the error so a wrong env value is
#: self-diagnosing.
MODEL_KIND = "voice_design"
OTHER_MODEL_KINDS = ("custom_voice", "base")


@dataclass(frozen=True)
class VoiceDesignResult:
    audio: bytes
    sample_rate: int
    model: str
    device: str
    language: str
    inference_ms: float
    audio_ms: float
    rtf: float
    cuda_allocated_mb: float | None = None
    cuda_reserved_mb: float | None = None
    cuda_peak_mb: float | None = None


class VoiceDesignRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    instruct: str = Field(default="", max_length=1000)
    language: str = Field(default=DEFAULT_LANGUAGE, max_length=32)
    max_new_tokens: int = Field(default=DEFAULT_MAX_NEW_TOKENS, ge=128, le=8192)


class VoiceDesignRuntime:
    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL,
        device: str = "auto",
        dtype: str = "auto",
        attn_implementation: str = "sdpa",
        language: str = DEFAULT_LANGUAGE,
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        preload: bool = False,
        model_loader: Callable[..., Any] | None = None,
        torch_module: Any | None = None,
    ):
        self.model_id = str(model_id or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.device_requested = str(device or "auto").strip().lower() or "auto"
        self.dtype_requested = str(dtype or "auto").strip().lower() or "auto"
        self.attn_implementation = str(attn_implementation or "sdpa").strip() or "sdpa"
        self.default_language = str(language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
        self.max_new_tokens = max(128, int(max_new_tokens))
        self.preload = bool(preload)
        self._model_loader = model_loader
        self._model = None
        # Injected in tests (symmetric with model_loader); imported on load otherwise.
        self._torch = torch_module
        self._device: str | None = None
        self._dtype_name: str | None = None
        self._load_ms: float | None = None
        self._load_error: str | None = None
        self._lock = threading.RLock()

    def _dependency_status(self) -> tuple[bool, str | None]:
        if self._model_loader is not None:
            return True, None
        missing = [name for name in ("torch", "qwen_tts") if importlib.util.find_spec(name) is None]
        if missing:
            return False, f"Missing isolated Qwen3-TTS dependencies: {', '.join(missing)}"
        return True, None

    def _import_torch(self):
        if self._torch is None:
            import torch

            self._torch = torch
        return self._torch

    def _resolve_device(self, torch) -> str:
        requested = self.device_requested
        if requested == "auto":
            return "cuda:0" if torch.cuda.is_available() else "cpu"
        if requested.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("VoiceDesign requested CUDA but torch.cuda.is_available() is false")
        return requested

    def _resolve_dtype(self, torch, device: str):
        requested = self.dtype_requested
        if requested == "auto":
            if device.startswith("cuda"):
                bf16_supported = getattr(torch.cuda, "is_bf16_supported", lambda: False)()
                requested = "bfloat16" if bf16_supported else "float32"
            else:
                requested = "float32"
        aliases = {
            "fp16": "float16",
            "float16": "float16",
            "bf16": "bfloat16",
            "bfloat16": "bfloat16",
            "fp32": "float32",
            "float32": "float32",
        }
        name = aliases.get(requested)
        if name is None:
            raise ValueError(f"Unsupported VoiceDesign dtype: {requested}")
        return name, getattr(torch, name)

    def _cuda_memory(self, *, peak: bool = False) -> tuple[float | None, float | None, float | None]:
        torch = self._torch
        if torch is None or not str(self._device or "").startswith("cuda"):
            return None, None, None
        try:
            allocated = torch.cuda.memory_allocated() / (1024 * 1024)
            reserved = torch.cuda.memory_reserved() / (1024 * 1024)
            peak_value = torch.cuda.max_memory_allocated() / (1024 * 1024) if peak else None
            return (
                round(allocated, 1),
                round(reserved, 1),
                round(peak_value, 1) if peak_value is not None else None,
            )
        except Exception:
            return None, None, None

    def _reset_cuda_peak(self) -> None:
        torch = self._torch
        if torch is None or not str(self._device or "").startswith("cuda"):
            return
        try:
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        except Exception:
            pass

    def _sync_cuda(self) -> None:
        torch = self._torch
        if torch is None or not str(self._device or "").startswith("cuda"):
            return
        try:
            torch.cuda.synchronize()
        except Exception:
            pass

    def _default_model_loader(self, *, model_id: str, device: str, dtype, attn_implementation: str):
        from qwen_tts import Qwen3TTSModel

        return Qwen3TTSModel.from_pretrained(
            model_id,
            device_map=device,
            dtype=dtype,
            attn_implementation=attn_implementation,
        )

    @staticmethod
    def _model_kind(model: Any) -> str | None:
        return getattr(getattr(model, "model", None), "tts_model_type", None)

    def status(self) -> dict:
        deps_ready, deps_reason = self._dependency_status()
        allocated, reserved, _peak = self._cuda_memory()
        reason = self._load_error or deps_reason
        return {
            "id": "qwen3-voice-design",
            "label": "Qwen3-TTS 1.7B VoiceDesign",
            # Ready means "can load", not "is loaded": loading lazily is the
            # normal resting state, since holding the model would evict GSV.
            "ready": bool((deps_ready or self._model_loader is not None) and not self._load_error),
            "loaded": self._model is not None,
            "model": self.model_id,
            "device": self._device or self.device_requested,
            "dtype": self._dtype_name or self.dtype_requested,
            "attn_implementation": self.attn_implementation,
            "language": self.default_language,
            "max_new_tokens": self.max_new_tokens,
            "load_ms": self._load_ms,
            "cuda_allocated_mb": allocated,
            "cuda_reserved_mb": reserved,
            "reason": reason,
            "note": "VoiceDesign is an asset-creation tool: not a realtime provider and never routed to.",
        }

    def load(self) -> dict:
        with self._lock:
            if self._model is not None:
                return {**self.status(), "already_loaded": True}

            ready, reason = self._dependency_status()
            if not ready and self._model_loader is None:
                raise RuntimeError(reason or "Qwen3-TTS dependencies are unavailable")

            torch = self._import_torch()
            device = self._resolve_device(torch)
            dtype_name, dtype = self._resolve_dtype(torch, device)
            self._device = device
            self._dtype_name = dtype_name
            if device.startswith("cuda"):
                torch.cuda.empty_cache()
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()

            started = time.perf_counter()
            loader = self._model_loader or self._default_model_loader
            try:
                model = loader(
                    model_id=self.model_id,
                    device=device,
                    dtype=dtype,
                    attn_implementation=self.attn_implementation,
                )
                kind = self._model_kind(model)
                if kind != MODEL_KIND:
                    raise RuntimeError(
                        f"{self.model_id} is a {kind!r} checkpoint, not a {MODEL_KIND} model. "
                        f"Set QWEN3_VOICE_DESIGN_MODEL to a ...-VoiceDesign checkpoint "
                        f"(one of {', '.join(OTHER_MODEL_KINDS)} was given)."
                    )
            except Exception as exc:
                self._model = None
                self._load_error = str(exc)
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"VoiceDesign model load failed: {exc}") from exc

            self._model = model
            self._load_error = None
            self._sync_cuda()
            self._load_ms = round((time.perf_counter() - started) * 1000.0, 1)
            return {**self.status(), "already_loaded": False}

    def unload(self) -> dict:
        with self._lock:
            model = self._model
            self._model = None
            self._load_error = None
            self._load_ms = None
            if model is not None:
                del model
            gc.collect()
            torch = self._torch
            if torch is not None and str(self._device or "").startswith("cuda"):
                try:
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                except Exception:
                    pass
            return {**self.status(), "unloaded": True}

    def generate(self, request: VoiceDesignRequest) -> VoiceDesignResult:
        value = request.text.strip()
        if not value:
            raise ValueError("empty VoiceDesign text")
        language = (request.language or self.default_language).strip() or self.default_language
        instruct = request.instruct.strip()
        max_new_tokens = int(request.max_new_tokens or self.max_new_tokens)

        with self._lock:
            self.load()
            self._reset_cuda_peak()
            started = time.perf_counter()
            try:
                wavs, sample_rate = self._model.generate_voice_design(
                    text=value,
                    instruct=instruct,
                    language=language,
                    max_new_tokens=max_new_tokens,
                )
                self._sync_cuda()
            except Exception as exc:
                raise RuntimeError(f"VoiceDesign generation failed: {exc}") from exc

            if not wavs:
                raise RuntimeError("VoiceDesign returned no audio")
            samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
            if not samples.size:
                raise RuntimeError("VoiceDesign returned empty audio")
            sample_rate = int(sample_rate or 0)
            if sample_rate <= 0:
                raise RuntimeError("VoiceDesign returned an invalid sample rate")
            inference_ms = (time.perf_counter() - started) * 1000.0
            audio_ms = len(samples) * 1000.0 / sample_rate
            allocated, reserved, peak = self._cuda_memory(peak=True)
            return VoiceDesignResult(
                audio=float_audio_to_wav(samples, sample_rate),
                sample_rate=sample_rate,
                model=self.model_id,
                device=self._device or self.device_requested,
                language=language,
                inference_ms=round(inference_ms, 1),
                audio_ms=round(audio_ms, 1),
                rtf=round(inference_ms / audio_ms, 4) if audio_ms > 0 else 0.0,
                cuda_allocated_mb=allocated,
                cuda_reserved_mb=reserved,
                cuda_peak_mb=peak,
            )


def create_voice_design_app(runtime: VoiceDesignRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("Qwen3 VoiceDesign sidecar requires FastAPI in its isolated environment") from exc

    engine = runtime or VoiceDesignRuntime(
        model_id=os.getenv("QWEN3_VOICE_DESIGN_MODEL", DEFAULT_MODEL),
        device=os.getenv("QWEN3_VOICE_DESIGN_DEVICE", "auto"),
        dtype=os.getenv("QWEN3_VOICE_DESIGN_DTYPE", "auto"),
        attn_implementation=os.getenv("QWEN3_VOICE_DESIGN_ATTN", "sdpa"),
        language=os.getenv("QWEN3_VOICE_DESIGN_LANGUAGE", DEFAULT_LANGUAGE),
        max_new_tokens=int(
            os.getenv("QWEN3_VOICE_DESIGN_MAX_NEW_TOKENS", str(DEFAULT_MAX_NEW_TOKENS))
        ),
        preload=os.getenv("QWEN3_VOICE_DESIGN_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    app = FastAPI(title="Character Memory Qwen3 VoiceDesign Experiment", version="0.1")
    app.state.voice_design = engine

    @on_app_event(app, "startup")
    def startup():
        if engine.preload:
            try:
                engine.load()
            except RuntimeError as exc:
                print(f"qwen3-voice-design preload failed: {exc}", flush=True)

    @app.get("/health")
    def health():
        # Always 200, even when ready is false. The TTS Lab adapter reads a non-2xx
        # status as "sidecar is not running", so a 503 here would replace the real
        # reason with a wrong remedy in the Workbench.
        return {"ok": True, **engine.status()}

    @app.post("/v1/load")
    def load():
        try:
            return engine.load()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/unload")
    def unload():
        try:
            return engine.unload()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/voice-design")
    def voice_design(request: VoiceDesignRequest):
        try:
            result = engine.generate(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                # Only the canonical names. The adapter falls back to X-TTS-* for
                # older peers; sending both would hide which one it actually read.
                "X-Voice-Design-Model": quote(result.model, safe="/:._-"),
                "X-Voice-Design-Device": result.device,
                "X-Voice-Design-Inference-Ms": str(result.inference_ms),
                "X-Voice-Design-Audio-Ms": str(result.audio_ms),
                "X-Voice-Design-Sample-Rate": str(result.sample_rate),
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-qwen3-voice-design")
    parser.add_argument("--host", default=os.getenv("QWEN3_VOICE_DESIGN_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("QWEN3_VOICE_DESIGN_PORT", str(DEFAULT_PORT)))
    )
    parser.add_argument("--model", default=os.getenv("QWEN3_VOICE_DESIGN_MODEL", DEFAULT_MODEL))
    parser.add_argument("--device", default=os.getenv("QWEN3_VOICE_DESIGN_DEVICE", "auto"))
    parser.add_argument("--dtype", default=os.getenv("QWEN3_VOICE_DESIGN_DTYPE", "auto"))
    parser.add_argument("--attn", default=os.getenv("QWEN3_VOICE_DESIGN_ATTN", "sdpa"))
    parser.add_argument(
        "--language", default=os.getenv("QWEN3_VOICE_DESIGN_LANGUAGE", DEFAULT_LANGUAGE)
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=int(os.getenv("QWEN3_VOICE_DESIGN_MAX_NEW_TOKENS", str(DEFAULT_MAX_NEW_TOKENS))),
    )
    parser.add_argument(
        "--preload",
        action="store_true",
        default=os.getenv("QWEN3_VOICE_DESIGN_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Qwen3 VoiceDesign isolated environment must contain FastAPI and uvicorn") from exc

    runtime = VoiceDesignRuntime(
        model_id=args.model,
        device=args.device,
        dtype=args.dtype,
        attn_implementation=args.attn,
        language=args.language,
        max_new_tokens=args.max_new_tokens,
        preload=args.preload,
    )
    print(
        f"qwen3-voice-design: http://{args.host}:{args.port} model={runtime.model_id} "
        f"device={args.device} preload={args.preload}",
        flush=True,
    )
    uvicorn.run(
        create_voice_design_app(runtime),
        host=args.host,
        port=args.port,
        reload=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
