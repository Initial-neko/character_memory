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


ROOT = Path(__file__).resolve().parents[2]


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
    media_type: str = "audio/wav"


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
            response = self.client.get(f"{self.base_url}/health", timeout=0.5)
            response.raise_for_status()
            health = response.json()
            # Browser-facing `tts` reports the configured formal route. The Lab
            # needs the underlying local Sherpa runtime when auditioning Sherpa.
            tts = health.get("tts_runtime") or health.get("tts") or {}
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
    MODEL_FILENAME = "kokoro-v1_1-zh.pth"
    SAMPLE_RATE = 24000
    # v1.1-zh uses numbered Chinese speaker packs. The old zf_xiaobei/... names
    # belong to the base Kokoro-82M repository and return 404 against v1.1-zh.
    DEFAULT_VOICES = ["zf_001", "zf_002", "zf_003", "zf_004"]

    def __init__(self, device: str = "cpu"):
        self.device = device or "cpu"
        self._pipeline = None
        self._voice_paths: dict[str, str] = {}
        self._lock = threading.RLock()
        self.hf_home = Path(os.getenv("HF_HOME", ROOT / "models" / "huggingface")).resolve()
        self.hub_cache = self.hf_home / "hub"

    @property
    def required_files(self) -> list[str]:
        return [
            "config.json",
            self.MODEL_FILENAME,
            *(f"voices/{voice}.pt" for voice in self.DEFAULT_VOICES),
        ]

    def _cached_files(self) -> dict[str, Path]:
        if importlib.util.find_spec("huggingface_hub") is None:
            return {}
        from huggingface_hub import try_to_load_from_cache

        cached: dict[str, Path] = {}
        for filename in self.required_files:
            value = try_to_load_from_cache(
                self.REPO_ID,
                filename,
                cache_dir=str(self.hub_cache),
            )
            if isinstance(value, str):
                path = Path(value)
                if path.is_file():
                    cached[filename] = path
        return cached

    def _ensure(self):
        if self._pipeline is not None:
            return self._pipeline
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            installed = importlib.util.find_spec("kokoro") is not None and importlib.util.find_spec("misaki") is not None
            if not installed:
                raise RuntimeError("Kokoro 未安装；执行 bash scripts/setup-tts-models.sh")

            cached = self._cached_files()
            missing = [filename for filename in self.required_files if filename not in cached]
            if missing:
                raise RuntimeError(
                    "Kokoro 模型未预下载或不完整；执行 bash scripts/setup-tts-models.sh。"
                    f" 缺少: {', '.join(missing)}"
                )

            try:
                from kokoro import KPipeline
                from kokoro.model import KModel
            except ImportError as exc:
                raise RuntimeError("Kokoro 导入失败；重新执行 bash scripts/setup-tts-models.sh") from exc

            # Give Kokoro explicit local files so request-time synthesis never
            # reaches Hugging Face. Network/model setup belongs to setup scripts.
            model = KModel(
                repo_id=self.REPO_ID,
                config=str(cached["config.json"]),
                model=str(cached[self.MODEL_FILENAME]),
            ).to(self.device).eval()
            self._pipeline = KPipeline(
                lang_code="z",
                repo_id=self.REPO_ID,
                model=model,
                device=self.device,
            )
            self._voice_paths = {
                voice: str(cached[f"voices/{voice}.pt"])
                for voice in self.DEFAULT_VOICES
            }
            return self._pipeline

    def status(self) -> dict:
        installed = (
            importlib.util.find_spec("kokoro") is not None
            and importlib.util.find_spec("misaki") is not None
            and importlib.util.find_spec("huggingface_hub") is not None
        )
        cached = self._cached_files() if installed else {}
        missing = [filename for filename in self.required_files if filename not in cached]
        ready = installed and not missing
        if not installed:
            reason = "Run bash scripts/setup-tts-models.sh to install the Kokoro stack."
        elif missing:
            reason = "Run bash scripts/setup-tts-models.sh to prefetch Kokoro model assets."
        else:
            reason = None
        return {
            "id": "kokoro",
            "label": "Kokoro 82M v1.1 zh",
            "ready": ready,
            "loaded": self._pipeline is not None,
            "voices": list(self.DEFAULT_VOICES),
            "default_voice": self.DEFAULT_VOICES[0],
            "supports_speed": True,
            "model": self.REPO_ID,
            "device": self.device,
            "reason": reason,
            "note": "模型与音色必须由 setup-tts-models.sh 预下载；生成请求不会联网下载。",
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
        if voice not in self.DEFAULT_VOICES:
            raise ValueError(f"Unknown Kokoro voice: {voice}")
        pipeline = self._ensure()
        started = time.perf_counter()
        chunks: list[np.ndarray] = []
        voice_path = self._voice_paths[voice]
        for item in pipeline(value, voice=voice_path, speed=float(speed), split_pattern=r"\n+"):
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


class Qwen3SidecarProvider:
    DEFAULT_VOICES = [
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

    def __init__(self, base_url: str = "http://127.0.0.1:9013", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=180.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict:
        fallback = {
            "id": "qwen3",
            "label": "Qwen3-TTS 0.6B",
            "ready": False,
            "loaded": False,
            "voices": list(self.DEFAULT_VOICES),
            "default_voice": self.DEFAULT_VOICES[0],
            "supports_speed": False,
            "reason": f"Qwen3-TTS sidecar is not running at {self.base_url}",
        }
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=3.0)
            response.raise_for_status()
            data = response.json()
            voices = data.get("voices") or list(self.DEFAULT_VOICES)
            return {
                **fallback,
                "ready": bool(data.get("ready", True)),
                "loaded": bool(data.get("loaded")),
                "voices": voices,
                "default_voice": data.get("default_voice") or voices[0],
                "model": data.get("model"),
                "device": data.get("device"),
                "reason": data.get("reason"),
                "note": data.get("note"),
            }
        except Exception:
            return fallback

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/v1/tts",
            json={
                "text": text,
                "voice": voice or self.DEFAULT_VOICES[0],
                "language": "Chinese",
                "speed": speed,
            },
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
            provider="qwen3",
            voice=response.headers.get("x-tts-voice", voice or self.DEFAULT_VOICES[0]),
            model=response.headers.get("x-tts-model", "Qwen3-TTS-12Hz-0.6B-CustomVoice"),
            device=response.headers.get("x-tts-device", "unknown"),
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )


class GsvSidecarProvider:
    DEFAULT_VOICES = ["murasame"]

    def __init__(self, base_url: str = "http://127.0.0.1:9014", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=180.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict:
        fallback = {
            "id": "gsv",
            "label": "GSV-TTS-Lite",
            "ready": False,
            "loaded": False,
            "voices": list(self.DEFAULT_VOICES),
            "default_voice": self.DEFAULT_VOICES[0],
            "supports_speed": True,
            "reason": f"GSV-TTS-Lite sidecar is not running at {self.base_url}",
            "note": "Available in Lab and formal routing; start the :9014 sidecar with the validated GSV environment.",
        }
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=0.5)
            response.raise_for_status()
            data = response.json()
            voices = data.get("voices") or list(self.DEFAULT_VOICES)
            return {
                **fallback,
                "ready": bool(data.get("ready")),
                "loaded": bool(data.get("loaded")),
                "voices": voices,
                "default_voice": data.get("default_voice") or voices[0],
                "model": data.get("model"),
                "device": data.get("device"),
                "reason": data.get("reason"),
                "note": data.get("note") or fallback["note"],
            }
        except Exception:
            return fallback

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        selected_voice = str(voice or self.DEFAULT_VOICES[0]).strip() or self.DEFAULT_VOICES[0]
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/v1/tts",
            json={
                "text": text,
                "voice": selected_voice,
                "language": "zh",
                "speed": speed,
            },
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
            provider="gsv",
            voice=response.headers.get("x-tts-voice", selected_voice),
            model=response.headers.get("x-tts-model", "GSV-TTS-Lite"),
            device=response.headers.get("x-tts-device", "unknown"),
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )


class EdgeTtsProvider:
    DEFAULT_VOICES = [
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunjianNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-YunyangNeural",
    ]
    SAMPLE_RATE = 24000
    BITRATE_BPS = 48000
    MODEL = "Microsoft Edge Read Aloud"

    def __init__(
        self,
        *,
        edge_module=None,
        volume: str = "+0%",
        pitch: str = "+0Hz",
        proxy: str | None = None,
        connect_timeout: int = 10,
        receive_timeout: int = 30,
    ):
        self._edge_module = edge_module
        self.volume = volume or "+0%"
        self.pitch = pitch or "+0Hz"
        self.proxy = proxy or None
        self.connect_timeout = int(connect_timeout)
        self.receive_timeout = int(receive_timeout)

    def _installed(self) -> bool:
        if self._edge_module is not None:
            return True
        try:
            return importlib.util.find_spec("edge_tts") is not None
        except (ImportError, ValueError):
            return False

    def _module(self):
        if self._edge_module is not None:
            return self._edge_module
        if not self._installed():
            raise RuntimeError("Edge TTS 未安装；执行 bash scripts/sync-all.sh 或 uv sync --extra tts-edge")
        import edge_tts
        return edge_tts

    @staticmethod
    def _rate(speed: float) -> str:
        percent = int(round((float(speed) - 1.0) * 100.0))
        return f"{percent:+d}%"

    def status(self) -> dict:
        installed = self._installed()
        return {
            "id": "edge",
            "label": "Microsoft Edge TTS (online)",
            "ready": installed,
            "loaded": installed,
            "voices": list(self.DEFAULT_VOICES),
            "default_voice": self.DEFAULT_VOICES[0],
            "supports_speed": True,
            "model": self.MODEL,
            "device": "cloud",
            "reason": None if installed else "Install the tts-edge extra to enable Edge TTS.",
            "network_required": True,
            "note": "在线 Provider，无需 API Key；合成时必须能访问 Microsoft Edge TTS 服务。",
        }

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        value = str(text or "").strip()
        if not value:
            raise ValueError("empty TTS text")
        selected_voice = str(voice or self.DEFAULT_VOICES[0]).strip()
        if selected_voice not in self.DEFAULT_VOICES:
            raise ValueError(f"Unknown Edge TTS voice: {selected_voice}")

        edge_tts = self._module()
        started = time.perf_counter()
        try:
            communicate = edge_tts.Communicate(
                value,
                selected_voice,
                rate=self._rate(speed),
                volume=self.volume,
                pitch=self.pitch,
                boundary="SentenceBoundary",
                proxy=self.proxy,
                connect_timeout=self.connect_timeout,
                receive_timeout=self.receive_timeout,
            )
            chunks: list[bytes] = []
            for chunk in communicate.stream_sync():
                if chunk.get("type") == "audio":
                    data = chunk.get("data") or b""
                    if data:
                        chunks.append(bytes(data))
        except Exception as exc:
            raise RuntimeError(f"Edge TTS synthesis failed: {exc}") from exc

        if not chunks:
            raise RuntimeError("Edge TTS returned no audio")
        audio = b"".join(chunks)
        inference_ms = (time.perf_counter() - started) * 1000.0
        audio_ms = len(audio) * 8.0 * 1000.0 / self.BITRATE_BPS
        return LabSynthesisResult(
            audio=audio,
            sample_rate=self.SAMPLE_RATE,
            provider="edge",
            voice=selected_voice,
            model=self.MODEL,
            device="cloud",
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
            media_type="audio/mpeg",
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
            response = self.client.get(f"{self.base_url}/health", timeout=0.5)
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
            "gsv": GsvSidecarProvider(os.getenv("CHARACTER_TTS_GSV_BASE", "http://127.0.0.1:9014")),
            "edge": EdgeTtsProvider(
                volume=os.getenv("CHARACTER_TTS_EDGE_VOLUME", "+0%"),
                pitch=os.getenv("CHARACTER_TTS_EDGE_PITCH", "+0Hz"),
                proxy=os.getenv("CHARACTER_TTS_EDGE_PROXY") or None,
            ),
            "cosyvoice": CosyVoiceSidecarProvider(os.getenv("CHARACTER_TTS_COSYVOICE_BASE", "http://127.0.0.1:9012")),
        }

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def statuses(self) -> list[dict]:
        return [provider.status() for provider in self.providers.values()]

    def provider_status(self, provider_id: str) -> dict:
        provider = self.providers.get(str(provider_id).strip().lower())
        if provider is None:
            raise ValueError(f"Unknown TTS provider: {provider_id}")
        return provider.status()

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

    @app.get("/v1/providers/{provider_id}")
    def provider(provider_id: str):
        try:
            return {"provider": lab.provider_status(provider_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
            media_type=result.media_type,
            headers={
                "X-TTS-Media-Type": result.media_type,
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
