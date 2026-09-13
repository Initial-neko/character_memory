from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import threading
import time
from typing import Protocol
from urllib.parse import quote

import httpx
import numpy as np
from pydantic import BaseModel, Field

from character_memory.media_runtime import float_audio_to_wav


@dataclass(frozen=True)
class LabSynthesisResult:
    audio: bytes
    sample_rate: int
    provider: str
    voice: str
    model: str
    device: str
    inference_ms: float
    audio_ms: float


class LabTtsProvider(Protocol):
    def status(self) -> dict: ...

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult: ...


class SherpaMediaProvider:
    def __init__(self, base_url: str = "http://127.0.0.1:8001", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict:
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=3.0)
            response.raise_for_status()
            health = response.json()
            tts = health.get("tts") or {}
            return {
                "id": "sherpa",
                "label": "Sherpa VITS (current baseline)",
                "ready": bool(tts.get("ready")),
                "loaded": bool(tts.get("loaded")),
                "voices": ["0", "2", "5"],
                "default_voice": "0",
                "supports_speed": True,
                "model": tts.get("model"),
                "device": tts.get("device", "cpu"),
                "reason": tts.get("reason"),
            }
        except Exception as exc:
            return {
                "id": "sherpa",
                "label": "Sherpa VITS (current baseline)",
                "ready": False,
                "loaded": False,
                "voices": ["0", "2", "5"],
                "default_voice": "0",
                "supports_speed": True,
                "reason": f"Media Runtime unavailable: {exc}",
            }

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        try:
            speaker_id = int(voice)
        except ValueError as exc:
            raise ValueError(f"Sherpa voice must be a numeric speaker id, got: {voice}") from exc
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/v1/tts",
            json={"text": text, "speaker_id": speaker_id, "speed": speed},
            timeout=120.0,
        )
        if response.is_error:
            raise RuntimeError(response.text)
        total_ms = (time.perf_counter() - started) * 1000.0
        inference_ms = float(response.headers.get("x-media-inference-ms") or total_ms)
        audio_ms = float(response.headers.get("x-media-audio-ms") or 0.0)
        sample_rate = int(response.headers.get("x-media-sample-rate") or 0)
        return LabSynthesisResult(
            audio=response.content,
            sample_rate=sample_rate,
            provider="sherpa",
            voice=str(speaker_id),
            model="sherpa-vits",
            device=response.headers.get("x-media-device", "cpu"),
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )


class KokoroProvider:
    REPO_ID = "hexgrad/Kokoro-82M-v1.1-zh"
    SAMPLE_RATE = 24000
    DEFAULT_VOICES = ["zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi"]

    def __init__(self, device: str = "cpu"):
        self.device = device or "cpu"
        self._pipeline = None
        self._lock = threading.RLock()

    def _ensure(self):
        if self._pipeline is not None:
            return self._pipeline
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise RuntimeError(
                    'Kokoro 未安装；执行 uv sync --extra api --extra media --extra tts-kokoro --extra dev'
                ) from exc
            self._pipeline = KPipeline(
                lang_code="z",
                repo_id=self.REPO_ID,
                device=self.device,
            )
            return self._pipeline

    def status(self) -> dict:
        installed = importlib.util.find_spec("kokoro") is not None and importlib.util.find_spec("misaki") is not None
        return {
            "id": "kokoro",
            "label": "Kokoro 82M v1.1 zh",
            "ready": installed,
            "loaded": self._pipeline is not None,
            "voices": list(self.DEFAULT_VOICES),
            "default_voice": "zf_xiaobei",
            "supports_speed": True,
            "model": self.REPO_ID,
            "device": self.device,
            "reason": None if installed else "Install the optional tts-kokoro extra first.",
            "note": "首次生成会从 Hugging Face 下载模型/音色并缓存。",
        }

    @staticmethod
    def _audio_array(item) -> np.ndarray | None:
        audio = getattr(item, "audio", None)
        if audio is None and isinstance(item, (tuple, list)) and len(item) >= 3:
            audio = item[2]
        if audio is None:
            return None
        if hasattr(audio, "detach"):
            audio = audio.detach()
        if hasattr(audio, "cpu"):
            audio = audio.cpu()
        if hasattr(audio, "numpy"):
            audio = audio.numpy()
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        value = str(text or "").strip()
        if not value:
            raise ValueError("empty TTS text")
        pipeline = self._ensure()
        started = time.perf_counter()
        chunks: list[np.ndarray] = []
        for item in pipeline(value, voice=voice, speed=float(speed), split_pattern=r"\n+"):
            audio = self._audio_array(item)
            if audio is not None and audio.size:
                chunks.append(audio)
        if not chunks:
            raise RuntimeError("Kokoro returned no audio")
        samples = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        inference_ms = (time.perf_counter() - started) * 1000.0
        audio_ms = len(samples) * 1000.0 / self.SAMPLE_RATE
        return LabSynthesisResult(
            audio=float_audio_to_wav(samples, self.SAMPLE_RATE),
            sample_rate=self.SAMPLE_RATE,
            provider="kokoro",
            voice=voice,
            model=self.REPO_ID,
            device=self.device,
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )


class CosyVoiceSidecarProvider:
    def __init__(self, base_url: str = "http://127.0.0.1:9012", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=180.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict:
        fallback = {
            "id": "cosyvoice",
            "label": "CosyVoice 300M SFT",
            "ready": False,
            "loaded": False,
            "voices": ["中文女"],
            "default_voice": "中文女",
            "supports_speed": False,
            "reason": f"CosyVoice sidecar is not running at {self.base_url}",
        }
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=3.0)
            response.raise_for_status()
            data = response.json()
            voices = data.get("voices") or ["中文女"]
            return {
                **fallback,
                "ready": bool(data.get("ready", True)),
                "loaded": bool(data.get("loaded")),
                "voices": voices,
                "default_voice": data.get("default_voice") or voices[0],
                "model": data.get("model"),
                "device": data.get("device"),
                "reason": data.get("reason"),
            }
        except Exception:
            return fallback

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/v1/tts",
            json={"text": text, "voice": voice, "speed": speed},
            timeout=180.0,
        )
        if response.is_error:
            raise RuntimeError(response.text)
        total_ms = (time.perf_counter() - started) * 1000.0
        inference_ms = float(response.headers.get("x-tts-inference-ms") or total_ms)
        audio_ms = float(response.headers.get("x-tts-audio-ms") or 0.0)
        sample_rate = int(response.headers.get("x-tts-sample-rate") or 0)
        return LabSynthesisResult(
            audio=response.content,
            sample_rate=sample_rate,
            provider="cosyvoice",
            voice=voice,
            model=response.headers.get("x-tts-model", "CosyVoice-300M-SFT"),
            device=response.headers.get("x-tts-device", "unknown"),
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )


class TtsLabRuntime:
    def __init__(self, providers: dict[str, LabTtsProvider] | None = None):
        self.providers = providers or {
            "sherpa": SherpaMediaProvider(os.getenv("CHARACTER_TTS_LAB_MEDIA_BASE", "http://127.0.0.1:8001")),
            "kokoro": KokoroProvider(os.getenv("CHARACTER_TTS_KOKORO_DEVICE", "cpu")),
            "cosyvoice": CosyVoiceSidecarProvider(os.getenv("CHARACTER_TTS_COSYVOICE_BASE", "http://127.0.0.1:9012")),
        }

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def statuses(self) -> list[dict]:
        return [provider.status() for provider in self.providers.values()]

    def synthesize(self, provider_id: str, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown TTS provider: {provider_id}")
        return provider.synthesize(text, voice=voice, speed=speed)


class TtsLabRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default="", max_length=128)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def create_tts_lab_app(runtime: TtsLabRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, Response
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("TTS Lab requires the api extra") from exc

    lab = runtime or TtsLabRuntime()
    web_dir = Path(__file__).with_name("web")
    app = FastAPI(title="Character Memory TTS Provider Lab", version="0.1")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("shutdown")
    def shutdown():
        lab.close()

    @app.get("/")
    @app.get("/tts")
    def index():
        return FileResponse(web_dir / "tts_lab.html")

    @app.get("/health")
    def health():
        return {"ok": True, "service": "character-tts-lab", "providers": lab.statuses()}

    @app.get("/v1/providers")
    def providers():
        return {"providers": lab.statuses()}

    @app.post("/v1/tts")
    def synthesize(req: TtsLabRequest):
        try:
            result = lab.synthesize(req.provider, req.text, voice=req.voice, speed=req.speed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-TTS-Provider": result.provider,
                "X-TTS-Voice": quote(result.voice, safe=""),
                "X-TTS-Model": quote(result.model, safe="/:._-"),
                "X-TTS-Device": result.device,
                "X-TTS-Inference-Ms": str(result.inference_ms),
                "X-TTS-Audio-Ms": str(result.audio_ms),
                "X-TTS-Sample-Rate": str(result.sample_rate),
            },
        )

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("TTS Lab requires the api extra") from exc
    host = os.getenv("CHARACTER_TTS_LAB_HOST", "127.0.0.1")
    port = int(os.getenv("CHARACTER_TTS_LAB_PORT", "9002"))
    print(f"tts-lab: http://{host}:{port}/tts")
    uvicorn.run(create_tts_lab_app(), host=host, port=port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
