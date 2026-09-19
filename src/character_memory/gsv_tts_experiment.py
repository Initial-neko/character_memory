from __future__ import annotations

import argparse
import gc
from dataclasses import dataclass
import importlib.util
import io
import os
from pathlib import Path
import threading
import time
import wave
from typing import Any, Callable
from urllib.parse import quote

import numpy as np
from pydantic import BaseModel, Field


DEFAULT_PORT = 9014
DEFAULT_VOICE = "murasame"
DEFAULT_SAMPLE_RATE = 32000


@dataclass(frozen=True)
class GsvTtsResult:
    audio: bytes
    sample_rate: int
    model: str
    device: str
    voice: str
    inference_ms: float
    audio_ms: float
    rtf: float
    cuda_allocated_mb: float | None = None
    cuda_reserved_mb: float | None = None
    cuda_peak_mb: float | None = None


class GsvTtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE, max_length=128)
    language: str = Field(default="zh", max_length=16)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class GsvRuntimeConfigRequest(BaseModel):
    gpt_model: str | None = None
    sovits_model: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    device: str | None = None
    models_dir: str | None = None
    voice: str | None = None
    language: str | None = None
    prompt_language: str | None = None
    preload: bool = False


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


class GsvTtsRuntime:
    """Isolated GSV-TTS-Lite runtime.

    Heavy gsv_tts/torch imports happen only inside this sidecar process.
    Character Memory talks to it through HTTP so the main environment stays
    independent from the CUDA/Torch ABI used by GSV-TTS-Lite.
    """

    def __init__(
        self,
        *,
        gpt_model: str,
        sovits_model: str,
        ref_audio: str,
        ref_text: str,
        device: str = "cuda",
        models_dir: str = "",
        default_voice: str = DEFAULT_VOICE,
        default_language: str = "zh",
        prompt_language: str = "auto",
        preload: bool = False,
        tts_factory: Callable[..., Any] | None = None,
    ):
        self.gpt_model = str(gpt_model or "").strip()
        self.sovits_model = str(sovits_model or "").strip()
        self.ref_audio = str(ref_audio or "").strip()
        self.ref_text = str(ref_text or "").strip()
        self.device_requested = str(device or "cuda").strip()
        self.models_dir = str(models_dir or "").strip()
        self.default_voice = str(default_voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
        self.default_language = str(default_language or "zh").strip() or "zh"
        self.prompt_language = str(prompt_language or "auto").strip() or "auto"
        self.preload = bool(preload)
        self._tts_factory = tts_factory
        self._tts = None
        self._torch = None
        self._load_ms: float | None = None
        self._load_error: str | None = None
        self._lock = threading.RLock()

    @property
    def model_label(self) -> str:
        gpt = Path(self.gpt_model).name if self.gpt_model else "unset-gpt"
        sovits = Path(self.sovits_model).name if self.sovits_model else "unset-sovits"
        return f"{gpt}+{sovits}"

    def _dependency_status(self) -> tuple[bool, str | None]:
        if self._tts_factory is not None:
            return True, None
        try:
            available = importlib.util.find_spec("gsv_tts") is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            return False, (
                "GSV-TTS-Lite is not importable. Start this sidecar with "
                "scripts/start-gsv-tts.sh so its external Python environment is used."
            )
        return True, None

    def _asset_status(self) -> tuple[bool, str | None]:
        required = {
            "GSV_TTS_GPT_MODEL": self.gpt_model,
            "GSV_TTS_SOVITS_MODEL": self.sovits_model,
            "GSV_TTS_REF_AUDIO": self.ref_audio,
        }
        missing_config = [name for name, value in required.items() if not value]
        if not self.ref_text:
            missing_config.append("GSV_TTS_REF_TEXT")
        if missing_config:
            return False, f"Missing GSV configuration: {', '.join(missing_config)}"

        missing_files = [value for value in required.values() if value and not Path(value).is_file()]
        if missing_files:
            return False, f"GSV asset not found: {', '.join(missing_files)}"
        return True, None

    def _factory(self):
        if self._tts_factory is not None:
            return self._tts_factory
        from gsv_tts import TTS

        return TTS

    def _import_torch(self) -> None:
        if self._tts_factory is not None or self._torch is not None:
            return
        import torch

        self._torch = torch

    def _device(self) -> str:
        if self._tts is not None:
            config = getattr(self._tts, "tts_config", None)
            device = getattr(config, "device", None)
            if device is not None:
                return str(device)
        return self.device_requested

    def _cuda_memory(self, *, peak: bool = False) -> tuple[float | None, float | None, float | None]:
        torch = self._torch
        if torch is None or not self._device().startswith("cuda"):
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
        if torch is None or not self._device().startswith("cuda"):
            return
        try:
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        except Exception:
            pass

    def _sync_cuda(self) -> None:
        torch = self._torch
        if torch is None or not self._device().startswith("cuda"):
            return
        try:
            torch.cuda.synchronize()
        except Exception:
            pass

    def status(self) -> dict:
        deps_ready, deps_reason = self._dependency_status()
        assets_ready, assets_reason = self._asset_status()
        allocated, reserved, _peak = self._cuda_memory()
        reason = self._load_error or deps_reason or assets_reason
        return {
            "id": "gsv",
            "label": "GSV-TTS-Lite",
            "ready": bool(deps_ready and assets_ready and not self._load_error),
            "loaded": self._tts is not None,
            "model": self.model_label,
            "device": self._device(),
            "voices": [self.default_voice],
            "default_voice": self.default_voice,
            "default_language": self.default_language,
            "supports_speed": True,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "load_ms": self._load_ms,
            "cuda_allocated_mb": allocated,
            "cuda_reserved_mb": reserved,
            "reason": reason,
            "note": "Full-WAV GSV sidecar used by both TTS Lab and formal :8001 routing.",
        }

    def load(self) -> dict:
        with self._lock:
            if self._tts is not None:
                return {**self.status(), "already_loaded": True}

            deps_ready, deps_reason = self._dependency_status()
            assets_ready, assets_reason = self._asset_status()
            if not deps_ready:
                raise RuntimeError(deps_reason or "GSV-TTS-Lite dependency unavailable")
            if not assets_ready:
                raise RuntimeError(assets_reason or "GSV-TTS-Lite assets unavailable")

            started = time.perf_counter()
            try:
                factory = self._factory()
                kwargs = {
                    "gpt_cache": [(1, 512), (1, 1024), (4, 512), (4, 1024)],
                    "sovits_cache": [50, 55],
                    "device": None if self.device_requested.lower() == "auto" else self.device_requested,
                    "use_bert": True,
                    "use_flash_attn": False,
                    "always_load_cnhubert": True,
                    "always_load_sv": True,
                }
                if self.models_dir:
                    kwargs["models_dir"] = self.models_dir
                engine = factory(**kwargs)
                engine.load_gpt_model(self.gpt_model)
                engine.load_sovits_model(self.sovits_model)
                engine.cache_spk_audio(self.ref_audio, sovits_model=self.sovits_model)
                engine.cache_prompt_audio(
                    prompt_audio_paths=self.ref_audio,
                    prompt_audio_texts=self.ref_text,
                    prompt_language=self.prompt_language,
                )
                self._tts = engine
                self._import_torch()
                self._sync_cuda()
                self._load_ms = round((time.perf_counter() - started) * 1000.0, 1)
                self._load_error = None
                return {**self.status(), "already_loaded": False}
            except Exception as exc:
                self._tts = None
                self._load_error = str(exc)
                raise RuntimeError(f"GSV-TTS-Lite load failed: {exc}") from exc

    def unload(self) -> dict:
        with self._lock:
            engine = self._tts
            self._tts = None
            self._load_error = None
            self._load_ms = None
            if engine is not None:
                del engine
            gc.collect()
            torch = self._torch
            if torch is not None and self._device().startswith("cuda"):
                try:
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                except Exception:
                    pass
            return {**self.status(), "unloaded": True}

    def configure(self, request: GsvRuntimeConfigRequest) -> dict:
        updates = {
            "gpt_model": request.gpt_model,
            "sovits_model": request.sovits_model,
            "ref_audio": request.ref_audio,
            "ref_text": request.ref_text,
            "device_requested": request.device,
            "models_dir": request.models_dir,
            "default_voice": request.voice,
            "default_language": request.language,
            "prompt_language": request.prompt_language,
        }
        changed: list[str] = []
        with self._lock:
            for attr, raw in updates.items():
                if raw is None:
                    continue
                value = str(raw).strip()
                if attr == "default_voice":
                    value = value or DEFAULT_VOICE
                elif attr == "default_language":
                    value = value or "zh"
                elif attr == "prompt_language":
                    value = value or "auto"
                elif attr == "device_requested":
                    value = value or "cuda"
                if getattr(self, attr) != value:
                    setattr(self, attr, value)
                    changed.append(attr)

            if changed and self._tts is not None:
                engine = self._tts
                self._tts = None
                self._load_error = None
                self._load_ms = None
                del engine
                gc.collect()
                torch = self._torch
                if torch is not None:
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
            elif changed:
                self._load_error = None
                self._load_ms = None

        status = self.status()
        if request.preload and status.get("ready"):
            status = self.load()
        return {**status, "changed": changed}

    def synthesize(self, request: GsvTtsRequest) -> GsvTtsResult:
        value = request.text.strip()
        if not value:
            raise ValueError("empty GSV-TTS text")
        voice = str(request.voice or self.default_voice).strip() or self.default_voice
        if voice != self.default_voice:
            raise ValueError(f"Unknown GSV voice profile: {voice}")

        with self._lock:
            self.load()
            self._reset_cuda_peak()
            started = time.perf_counter()
            try:
                clips = self._tts.infer_batched(
                    spk_audio_paths=self.ref_audio,
                    prompt_audio_paths=self.ref_audio,
                    prompt_audio_texts=self.ref_text,
                    texts=value,
                    text_languages=(request.language or self.default_language).strip() or self.default_language,
                    prompt_languages=self.prompt_language,
                    return_subtitles=False,
                    speed=float(request.speed),
                    gpt_model=self.gpt_model,
                    sovits_model=self.sovits_model,
                )
                self._sync_cuda()
            except Exception as exc:
                raise RuntimeError(f"GSV-TTS-Lite synthesis failed: {exc}") from exc

            if not clips:
                raise RuntimeError("GSV-TTS-Lite returned no audio")
            clip = clips[0]
            raw_audio = getattr(clip, "audio_data", None)
            if raw_audio is None:
                raise RuntimeError("GSV-TTS-Lite returned a clip without audio_data")
            samples = np.asarray(raw_audio, dtype=np.float32).reshape(-1)
            if not samples.size:
                raise RuntimeError("GSV-TTS-Lite returned empty audio")
            sample_rate = int(getattr(clip, "samplerate", DEFAULT_SAMPLE_RATE) or DEFAULT_SAMPLE_RATE)
            inference_ms = (time.perf_counter() - started) * 1000.0
            audio_ms = len(samples) * 1000.0 / sample_rate
            allocated, reserved, peak = self._cuda_memory(peak=True)
            return GsvTtsResult(
                audio=float_audio_to_wav(samples, sample_rate),
                sample_rate=sample_rate,
                model=self.model_label,
                device=self._device(),
                voice=voice,
                inference_ms=round(inference_ms, 1),
                audio_ms=round(audio_ms, 1),
                rtf=round(inference_ms / audio_ms, 4) if audio_ms > 0 else 0.0,
                cuda_allocated_mb=allocated,
                cuda_reserved_mb=reserved,
                cuda_peak_mb=peak,
            )


def create_gsv_tts_app(runtime: GsvTtsRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("GSV-TTS-Lite sidecar requires FastAPI in its isolated environment") from exc

    engine = runtime or GsvTtsRuntime(
        gpt_model=os.getenv("GSV_TTS_GPT_MODEL", ""),
        sovits_model=os.getenv("GSV_TTS_SOVITS_MODEL", ""),
        ref_audio=os.getenv("GSV_TTS_REF_AUDIO", ""),
        ref_text=os.getenv("GSV_TTS_REF_TEXT", ""),
        device=os.getenv("GSV_TTS_DEVICE", "cuda"),
        models_dir=os.getenv("GSV_TTS_MODELS_DIR", ""),
        default_voice=os.getenv("GSV_TTS_VOICE", DEFAULT_VOICE),
        default_language=os.getenv("GSV_TTS_LANGUAGE", "zh"),
        prompt_language=os.getenv("GSV_TTS_PROMPT_LANGUAGE", "auto"),
        preload=os.getenv("GSV_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    app = FastAPI(title="Character Memory GSV-TTS-Lite Experiment", version="0.1")
    app.state.gsv_tts = engine

    @app.on_event("startup")
    def startup():
        if engine.preload:
            try:
                engine.load()
            except RuntimeError as exc:
                print(f"gsv-tts preload failed: {exc}", flush=True)

    @app.get("/health")
    def health():
        return {"ok": True, **engine.status()}

    @app.post("/v1/load")
    def load():
        try:
            return engine.load()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/configure")
    def configure(request: GsvRuntimeConfigRequest):
        try:
            return engine.configure(request)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/unload")
    def unload():
        try:
            return engine.unload()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/tts")
    def synthesize(request: GsvTtsRequest):
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
                "X-TTS-Provider": "gsv",
                "X-TTS-Voice": quote(result.voice, safe=""),
                "X-TTS-Model": quote(result.model, safe="/:._-"),
                "X-TTS-Device": result.device,
                "X-TTS-Inference-Ms": str(result.inference_ms),
                "X-TTS-Audio-Ms": str(result.audio_ms),
                "X-TTS-RTF": str(result.rtf),
                "X-TTS-Sample-Rate": str(result.sample_rate),
                "X-TTS-Cuda-Allocated-MB": "" if result.cuda_allocated_mb is None else str(result.cuda_allocated_mb),
                "X-TTS-Cuda-Reserved-MB": "" if result.cuda_reserved_mb is None else str(result.cuda_reserved_mb),
                "X-TTS-Cuda-Peak-MB": "" if result.cuda_peak_mb is None else str(result.cuda_peak_mb),
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-gsv-tts")
    parser.add_argument("--host", default=os.getenv("GSV_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GSV_TTS_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--gpt-model", default=os.getenv("GSV_TTS_GPT_MODEL", ""))
    parser.add_argument("--sovits-model", default=os.getenv("GSV_TTS_SOVITS_MODEL", ""))
    parser.add_argument("--ref-audio", default=os.getenv("GSV_TTS_REF_AUDIO", ""))
    parser.add_argument("--ref-text", default=os.getenv("GSV_TTS_REF_TEXT", ""))
    parser.add_argument("--device", default=os.getenv("GSV_TTS_DEVICE", "cuda"))
    parser.add_argument("--models-dir", default=os.getenv("GSV_TTS_MODELS_DIR", ""))
    parser.add_argument("--voice", default=os.getenv("GSV_TTS_VOICE", DEFAULT_VOICE))
    parser.add_argument("--language", default=os.getenv("GSV_TTS_LANGUAGE", "zh"))
    parser.add_argument("--prompt-language", default=os.getenv("GSV_TTS_PROMPT_LANGUAGE", "auto"))
    parser.add_argument(
        "--preload",
        action="store_true",
        default=os.getenv("GSV_TTS_PRELOAD", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("GSV-TTS-Lite isolated environment must contain FastAPI and uvicorn") from exc

    runtime = GsvTtsRuntime(
        gpt_model=args.gpt_model,
        sovits_model=args.sovits_model,
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        device=args.device,
        models_dir=args.models_dir,
        default_voice=args.voice,
        default_language=args.language,
        prompt_language=args.prompt_language,
        preload=args.preload,
    )
    print(
        f"gsv-tts: http://{args.host}:{args.port} model={runtime.model_label} "
        f"device={args.device} preload={args.preload}",
        flush=True,
    )
    uvicorn.run(create_gsv_tts_app(runtime), host=args.host, port=args.port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
