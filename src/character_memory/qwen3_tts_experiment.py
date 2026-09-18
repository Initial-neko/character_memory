from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import io
import os
import threading
import time
import wave
from typing import Any, Callable
from urllib.parse import quote

import numpy as np
from pydantic import BaseModel, Field


DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
DEFAULT_SPEAKER = "Vivian"
CUSTOM_VOICE_SPEAKERS = [
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
]


@dataclass(frozen=True)
class Qwen3TtsResult:
    audio: bytes
    sample_rate: int
    model: str
    device: str
    dtype: str
    attn_implementation: str
    voice: str
    mode: str
    inference_ms: float
    audio_ms: float
    rtf: float
    cuda_allocated_mb: float | None = None
    cuda_reserved_mb: float | None = None
    cuda_peak_mb: float | None = None


class Qwen3TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_SPEAKER, max_length=128)
    language: str = Field(default="Chinese", max_length=32)
    instruct: str = Field(default="", max_length=1000)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    ref_audio: str | None = Field(default=None, max_length=2048)
    ref_text: str | None = Field(default=None, max_length=4000)
    x_vector_only_mode: bool | None = None


def float_audio_to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    values = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return output.getvalue()


class Qwen3TtsRuntime:
    """Isolated Qwen3-TTS runtime used by the formal TTS provider route.

    Heavy Torch/Qwen imports happen only inside this sidecar process and only
    when the model is explicitly loaded or the first synthesis request arrives.
    Media Runtime talks to this process over HTTP so Torch/CUDA stays isolated
    from Character Memory's core Python environment.
    """

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL,
        device: str = "auto",
        dtype: str = "auto",
        attn_implementation: str = "sdpa",
        default_speaker: str = DEFAULT_SPEAKER,
        default_language: str = "Chinese",
        ref_audio: str = "",
        ref_text: str = "",
        max_new_tokens: int = 2048,
        preload: bool = False,
        model_loader: Callable[..., Any] | None = None,
    ):
        self.model_id = str(model_id or DEFAULT_MODEL).strip()
        self.device_requested = str(device or "auto").strip().lower()
        self.dtype_requested = str(dtype or "auto").strip().lower()
        self.attn_implementation = str(attn_implementation or "sdpa").strip()
        self.default_speaker = str(default_speaker or DEFAULT_SPEAKER).strip()
        self.default_language = str(default_language or "Chinese").strip()
        self.ref_audio = str(ref_audio or "").strip()
        self.ref_text = str(ref_text or "").strip()
        self.max_new_tokens = max(128, int(max_new_tokens))
        self.preload = bool(preload)
        self._model_loader = model_loader
        self._model = None
        self._torch = None
        self._device = None
        self._dtype_name = None
        self._clone_prompt = None
        self._load_ms = None
        self._load_cuda_peak_mb = None
        self._lock = threading.RLock()

    @property
    def mode(self) -> str:
        lower = self.model_id.lower()
        if lower.endswith("-base") or lower.endswith("base"):
            return "clone"
        if "customvoice" in lower:
            return "custom"
        return "unknown"

    def _dependency_status(self) -> tuple[bool, str | None]:
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
            raise RuntimeError("Qwen3-TTS requested CUDA but torch.cuda.is_available() is false")
        return requested

    def _resolve_dtype(self, torch, device: str):
        requested = self.dtype_requested
        if requested == "auto":
            requested = "float16" if device.startswith("cuda") else "float32"
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
            raise ValueError(f"Unsupported Qwen3-TTS dtype: {requested}")
        return name, getattr(torch, name)

    def _cuda_memory(self, *, peak: bool = False) -> tuple[float | None, float | None, float | None]:
        torch = self._torch
        if torch is None or not str(self._device or "").startswith("cuda"):
            return None, None, None
        allocated = torch.cuda.memory_allocated() / (1024 * 1024)
        reserved = torch.cuda.memory_reserved() / (1024 * 1024)
        peak_value = torch.cuda.max_memory_allocated() / (1024 * 1024) if peak else None
        return round(allocated, 1), round(reserved, 1), round(peak_value, 1) if peak_value is not None else None

    def _sync_cuda(self) -> None:
        if self._torch is not None and str(self._device or "").startswith("cuda"):
            self._torch.cuda.synchronize()

    def _default_model_loader(self, *, model_id: str, device: str, dtype, attn_implementation: str):
        from qwen_tts import Qwen3TTSModel

        return Qwen3TTSModel.from_pretrained(
            model_id,
            device_map=device,
            dtype=dtype,
            attn_implementation=attn_implementation,
        )

    def _build_clone_prompt(self, ref_audio: str, ref_text: str | None, x_vector_only_mode: bool):
        if self._model is None:
            raise RuntimeError("Qwen3-TTS model is not loaded")
        return self._model.create_voice_clone_prompt(
            ref_audio=ref_audio,
            ref_text=ref_text or None,
            x_vector_only_mode=x_vector_only_mode,
        )

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
            self._model = loader(
                model_id=self.model_id,
                device=device,
                dtype=dtype,
                attn_implementation=self.attn_implementation,
            )
            if self.mode == "clone" and self.ref_audio:
                self._clone_prompt = self._build_clone_prompt(
                    self.ref_audio,
                    self.ref_text or None,
                    x_vector_only_mode=not bool(self.ref_text),
                )
            self._sync_cuda()
            self._load_ms = round((time.perf_counter() - started) * 1000.0, 1)
            _allocated, _reserved, peak = self._cuda_memory(peak=True)
            self._load_cuda_peak_mb = peak
            return {**self.status(), "already_loaded": False}

    def status(self) -> dict:
        dependencies_ready, reason = self._dependency_status()
        voices = list(CUSTOM_VOICE_SPEAKERS) if self.mode == "custom" else ["clone"]
        cuda_available = None
        if self._torch is not None:
            cuda_available = bool(self._torch.cuda.is_available())
        allocated, reserved, _peak = self._cuda_memory(peak=False)
        return {
            "id": "qwen3",
            "label": "Qwen3-TTS 0.6B",
            "ready": bool(dependencies_ready or self._model_loader is not None),
            "loaded": self._model is not None,
            "mode": self.mode,
            "model": self.model_id,
            "device": self._device or self.device_requested,
            "dtype": self._dtype_name or self.dtype_requested,
            "attn_implementation": self.attn_implementation,
            "voices": voices,
            "default_voice": self.default_speaker if self.mode == "custom" else "clone",
            "default_language": self.default_language,
            "supports_speed": False,
            "cuda_available": cuda_available,
            "load_ms": self._load_ms,
            "load_cuda_peak_mb": self._load_cuda_peak_mb,
            "cuda_allocated_mb": allocated,
            "cuda_reserved_mb": reserved,
            "reason": reason,
            "note": "Formal Qwen3 provider sidecar; Media Runtime routes to this process over HTTP.",
        }

    def synthesize(self, request: Qwen3TtsRequest) -> Qwen3TtsResult:
        value = request.text.strip()
        if not value:
            raise ValueError("empty Qwen3-TTS text")
        with self._lock:
            self.load()
            torch = self._torch
            if torch is not None and str(self._device).startswith("cuda"):
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()

            started = time.perf_counter()
            try:
                if self.mode == "custom":
                    speaker = (request.voice or self.default_speaker).strip() or self.default_speaker
                    if speaker not in CUSTOM_VOICE_SPEAKERS:
                        raise ValueError(f"Unknown Qwen3-TTS CustomVoice speaker: {speaker}")
                    wavs, sample_rate = self._model.generate_custom_voice(
                        text=value,
                        language=(request.language or self.default_language).strip() or self.default_language,
                        speaker=speaker,
                        instruct=(request.instruct or "").strip(),
                        max_new_tokens=self.max_new_tokens,
                    )
                    voice = speaker
                elif self.mode == "clone":
                    ref_audio = str(request.ref_audio or "").strip()
                    ref_text = str(request.ref_text or "").strip()
                    prompt = self._clone_prompt
                    if ref_audio:
                        xvector = request.x_vector_only_mode
                        if xvector is None:
                            xvector = not bool(ref_text)
                        prompt = self._build_clone_prompt(ref_audio, ref_text or None, bool(xvector))
                    if prompt is None:
                        raise ValueError(
                            "Base model requires QWEN3_TTS_REF_AUDIO or request.ref_audio; "
                            "ref_text is optional when x-vector-only mode is used"
                        )
                    wavs, sample_rate = self._model.generate_voice_clone(
                        text=value,
                        language=(request.language or self.default_language).strip() or self.default_language,
                        voice_clone_prompt=prompt,
                        max_new_tokens=self.max_new_tokens,
                    )
                    voice = "clone"
                else:
                    raise RuntimeError(f"Unsupported Qwen3-TTS model family: {self.model_id}")
                self._sync_cuda()
            except Exception as exc:
                if torch is not None and hasattr(torch, "cuda") and str(self._device).startswith("cuda"):
                    oom_type = getattr(torch.cuda, "OutOfMemoryError", ())
                    if oom_type and isinstance(exc, oom_type):
                        torch.cuda.empty_cache()
                        raise RuntimeError(
                            "Qwen3-TTS CUDA OOM. Try the 0.6B model, float16, SDPA, or free GPU memory."
                        ) from exc
                raise

            if not wavs:
                raise RuntimeError("Qwen3-TTS returned no audio")
            samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
            inference_ms = (time.perf_counter() - started) * 1000.0
            audio_ms = len(samples) * 1000.0 / int(sample_rate)
            allocated, reserved, peak = self._cuda_memory(peak=True)
            rtf = inference_ms / audio_ms if audio_ms > 0 else 0.0
            return Qwen3TtsResult(
                audio=float_audio_to_wav(samples, int(sample_rate)),
                sample_rate=int(sample_rate),
                model=self.model_id,
                device=str(self._device),
                dtype=str(self._dtype_name),
                attn_implementation=self.attn_implementation,
                voice=voice,
                mode=self.mode,
                inference_ms=round(inference_ms, 1),
                audio_ms=round(audio_ms, 1),
                rtf=round(rtf, 4),
                cuda_allocated_mb=allocated,
                cuda_reserved_mb=reserved,
                cuda_peak_mb=peak,
            )


def create_qwen3_tts_app(runtime: Qwen3TtsRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("Qwen3-TTS sidecar requires FastAPI in its isolated environment") from exc

    engine = runtime or Qwen3TtsRuntime(
        model_id=os.getenv("QWEN3_TTS_MODEL", DEFAULT_MODEL),
        device=os.getenv("QWEN3_TTS_DEVICE", "auto"),
        dtype=os.getenv("QWEN3_TTS_DTYPE", "auto"),
        attn_implementation=os.getenv("QWEN3_TTS_ATTN", "sdpa"),
        default_speaker=os.getenv("QWEN3_TTS_SPEAKER", DEFAULT_SPEAKER),
        default_language=os.getenv("QWEN3_TTS_LANGUAGE", "Chinese"),
        ref_audio=os.getenv("QWEN3_TTS_REF_AUDIO", ""),
        ref_text=os.getenv("QWEN3_TTS_REF_TEXT", ""),
        max_new_tokens=int(os.getenv("QWEN3_TTS_MAX_NEW_TOKENS", "2048")),
        preload=os.getenv("QWEN3_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    app = FastAPI(title="Character Memory Qwen3-TTS Experiment", version="0.1")
    app.state.qwen3_tts = engine

    @app.on_event("startup")
    def startup():
        if engine.preload:
            engine.load()

    @app.get("/health")
    def health():
        return {"ok": True, **engine.status()}

    @app.post("/v1/load")
    def load():
        try:
            return engine.load()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/tts")
    def synthesize(request: Qwen3TtsRequest):
        try:
            result = engine.synthesize(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-TTS-Provider": "qwen3",
                "X-TTS-Voice": quote(result.voice, safe=""),
                "X-TTS-Model": quote(result.model, safe="/:._-"),
                "X-TTS-Device": result.device,
                "X-TTS-Dtype": result.dtype,
                "X-TTS-Attn": result.attn_implementation,
                "X-TTS-Mode": result.mode,
                "X-TTS-Inference-Ms": str(result.inference_ms),
                "X-TTS-Audio-Ms": str(result.audio_ms),
                "X-TTS-RTF": str(result.rtf),
                "X-TTS-Sample-Rate": str(result.sample_rate),
                "X-TTS-Cuda-Allocated-MB": "" if result.cuda_allocated_mb is None else str(result.cuda_allocated_mb),
                "X-TTS-Cuda-Reserved-MB": "" if result.cuda_reserved_mb is None else str(result.cuda_reserved_mb),
                "X-TTS-Cuda-Peak-MB": "" if result.cuda_peak_mb is None else str(result.cuda_peak_mb),
                "X-TTS-Speed-Ignored": "true" if request.speed != 1.0 else "false",
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-qwen3-tts")
    parser.add_argument("--host", default=os.getenv("QWEN3_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QWEN3_TTS_PORT", "9013")))
    parser.add_argument("--model", default=os.getenv("QWEN3_TTS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--device", default=os.getenv("QWEN3_TTS_DEVICE", "auto"))
    parser.add_argument("--dtype", default=os.getenv("QWEN3_TTS_DTYPE", "auto"))
    parser.add_argument("--attn", default=os.getenv("QWEN3_TTS_ATTN", "sdpa"))
    parser.add_argument("--speaker", default=os.getenv("QWEN3_TTS_SPEAKER", DEFAULT_SPEAKER))
    parser.add_argument("--language", default=os.getenv("QWEN3_TTS_LANGUAGE", "Chinese"))
    parser.add_argument("--ref-audio", default=os.getenv("QWEN3_TTS_REF_AUDIO", ""))
    parser.add_argument("--ref-text", default=os.getenv("QWEN3_TTS_REF_TEXT", ""))
    parser.add_argument("--max-new-tokens", type=int, default=int(os.getenv("QWEN3_TTS_MAX_NEW_TOKENS", "2048")))
    parser.add_argument("--preload", action="store_true", default=os.getenv("QWEN3_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"})
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Run bash scripts/setup-qwen3-tts.sh first") from exc

    runtime = Qwen3TtsRuntime(
        model_id=args.model,
        device=args.device,
        dtype=args.dtype,
        attn_implementation=args.attn,
        default_speaker=args.speaker,
        default_language=args.language,
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        max_new_tokens=args.max_new_tokens,
        preload=args.preload,
    )
    print(f"qwen3-tts: http://{args.host}:{args.port} model={args.model} device={args.device} dtype={args.dtype}", flush=True)
    uvicorn.run(create_qwen3_tts_app(runtime), host=args.host, port=args.port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
