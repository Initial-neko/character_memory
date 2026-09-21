from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Protocol
from urllib.parse import quote

import httpx
import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from character_memory.media_runtime import float_audio_to_wav
from character_memory.tts_registry import provider_spec
from character_memory.voices import template_root
from character_memory.web_assets import attach_static_assets


ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("character_memory.tts_lab")


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
                "voices": list(provider_spec("sherpa").voices),
                "default_voice": provider_spec("sherpa").default_voice,
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
            f"{self.base_url}/v1/providers/sherpa/tts",
            json={"text": text, "speaker_id": speaker_id, "voice": str(speaker_id), "speed": speed},
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
    DEFAULT_VOICES = list(provider_spec("kokoro").voices)

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


class GsvSidecarProvider:
    DEFAULT_VOICES = list(provider_spec("gsv").voices)

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
                # Carried, not folded into ``reason``: the sidecar stays ready
                # without a usable default template, and only the requests that
                # fall back to it fail.
                "default_template_problem": data.get("default_template_problem"),
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
    DEFAULT_VOICES = list(provider_spec("edge").voices)
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


class Qwen3VoiceDesignSidecar:
    """Optional Qwen3-TTS 1.7B VoiceDesign tool adapter.

    This is deliberately separate from realtime TTS providers. The normal
    Character stack never selects it through tts_provider.
    """

    MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

    def __init__(self, base_url: str = "http://127.0.0.1:9015", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=300.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict:
        fallback = {
            "id": "qwen3-voice-design",
            "label": "Qwen3-TTS 1.7B VoiceDesign",
            "ready": False,
            "loaded": False,
            "model": self.MODEL,
            "device": None,
            "reason": f"VoiceDesign sidecar is not running at {self.base_url}",
            "base_url": self.base_url,
        }
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=0.8)
            response.raise_for_status()
            data = response.json() or {}
            return {
                **fallback,
                "ready": bool(data.get("ready")),
                "loaded": bool(data.get("loaded")),
                "model": data.get("model") or self.MODEL,
                "device": data.get("device"),
                "reason": data.get("reason"),
                "base_url": self.base_url,
            }
        except Exception:
            return fallback

    def generate(
        self,
        text: str,
        *,
        language: str,
        instruct: str,
        max_new_tokens: int = 2048,
    ) -> LabSynthesisResult:
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base_url}/v1/voice-design",
            json={
                "text": text,
                "language": language,
                "instruct": instruct,
                "max_new_tokens": int(max_new_tokens),
            },
            timeout=300.0,
        )
        if response.is_error:
            raise RuntimeError(response.text or f"VoiceDesign sidecar returned HTTP {response.status_code}")
        total_ms = (time.perf_counter() - started) * 1000.0
        headers = response.headers
        inference_ms = float(
            headers.get("x-voice-design-inference-ms")
            or headers.get("x-tts-inference-ms")
            or total_ms
        )
        audio_ms = float(
            headers.get("x-voice-design-audio-ms")
            or headers.get("x-tts-audio-ms")
            or 0.0
        )
        sample_rate = int(
            headers.get("x-voice-design-sample-rate")
            or headers.get("x-tts-sample-rate")
            or 24000
        )
        model = (
            headers.get("x-voice-design-model")
            or headers.get("x-tts-model")
            or self.MODEL
        )
        device = (
            headers.get("x-voice-design-device")
            or headers.get("x-tts-device")
            or "unknown"
        )
        return LabSynthesisResult(
            audio=response.content,
            sample_rate=sample_rate,
            provider="qwen3-voice-design",
            voice="designed",
            model=model,
            device=device,
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
            media_type=(headers.get("content-type") or "audio/wav").split(";", 1)[0],
        )


VOICE_DESIGN_ARTIFACT_LIMIT = 8


@dataclass(frozen=True)
class VoiceDesignArtifact:
    """One auditioned VoiceDesign output, kept byte-for-byte.

    VoiceDesign is unseeded: repeating the same request produces a different
    voice. The auditioned bytes are therefore the only copy of that voice, so
    freezing persists these exact bytes instead of re-synthesizing from the
    text and instruct.
    """

    audio: bytes
    text: str
    language: str
    instruct: str
    sample_rate: int
    model: str


class VoiceDesignArtifactStore:
    """Bounded in-memory store of auditioned VoiceDesign outputs.

    Tokens are opaque and server-chosen; the caller can only reference an
    audition, never define one. The oldest audition is dropped past the limit,
    which the freeze route reports as a 404 so the user regenerates.
    """

    def __init__(self, max_entries: int = VOICE_DESIGN_ARTIFACT_LIMIT):
        self._max_entries = max(1, int(max_entries))
        self._entries: dict[str, VoiceDesignArtifact] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    def put(self, artifact: VoiceDesignArtifact) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._entries[token] = artifact
            self._order.append(token)
            while len(self._order) > self._max_entries:
                self._entries.pop(self._order.popleft(), None)
        return token

    def get(self, token: str) -> VoiceDesignArtifact | None:
        with self._lock:
            return self._entries.get(str(token or "").strip())


_TEMPLATE_NAME_MAX = 64
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_template_name(raw: str) -> str:
    """Validate a user-chosen template name.

    Unlike ``character_id`` -- whose safety comes from a whitelist lookup -- a
    template name is new, so it needs explicit rules. Each one below maps to a
    real failure: the name becomes ``voices/<name>.yaml`` and ``voices/<name>/``,
    so a separator escapes the tree and a Windows reserved word fails the write
    in a way that is hard to read.
    """

    name = str(raw or "").strip()
    if not name:
        raise ValueError("模板名不能为空")
    if len(name) > _TEMPLATE_NAME_MAX:
        raise ValueError(f"模板名 too long (max {_TEMPLATE_NAME_MAX})")
    if "/" in name or "\\" in name:
        raise ValueError("模板名不能包含路径分隔符")
    if name in {".", ".."} or ".." in name:
        raise ValueError("模板名不能包含 '..'")
    if Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise ValueError("模板名不能是绝对路径")
    if name.upper().split(".")[0] in _WINDOWS_RESERVED:
        raise ValueError(f"{name} 是 Windows 保留名，请换一个")
    if name != name.rstrip(". "):
        raise ValueError("模板名不能以 '.' 或空格结尾")
    return name


def _template_name_for(character_id: str, persona_dir: Path) -> str:
    """The name a character's frozen template is filed under.

    ``character_id`` is what the user sees, and it is the right name almost
    always -- but it is not necessarily a *filename*, and the freeze route names
    files with it. ``discover_character_profiles`` answers "is this a character
    the app shows", not "is this safe to join onto a directory": ``persona.yaml``
    may declare ``id: ../../escaped`` and still be discoverable. So the id must
    pass the same rule a typed name does, and the persona directory name -- which
    came from a glob of ``*/persona.yaml`` and therefore cannot contain a
    separator -- is the fallback for one that does not.
    """

    try:
        return validate_template_name(character_id)
    except ValueError:
        return persona_dir.name


def _write_voice_template(voices_root: Path, name: str, artifact: VoiceDesignArtifact) -> str:
    """Write the clip and the template document; return the relative ref_audio.

    Shared by the freeze route and the save-as-template route. They differ only
    in how ``name`` is chosen and whether a reference is written afterwards --
    everything about *how a template is persisted* is here, so the two writers
    cannot drift into emitting two shapes.

    Content-addressed: designs cannot be regenerated, so re-writing must never
    overwrite the clip an older template still points at. An orphaned WAV costs
    disk; a clobbered one loses a voice permanently.
    """

    template_dir = voices_root / name
    template_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(artifact.audio).hexdigest()[:16]
    audio_path = template_dir / f"{digest}.wav"
    if not audio_path.exists():
        audio_temp = audio_path.with_suffix(".wav.tmp")
        audio_temp.write_bytes(artifact.audio)
        audio_temp.replace(audio_path)

    # Relative to the template file, which is how ``load_template`` resolves it.
    # An absolute path would work too, but a relative path is what survives
    # moving the tree.
    ref_audio = f"{name}/{audio_path.name}"

    template_path = voices_root / f"{name}.yaml"
    template_temp = template_path.with_suffix(".yaml.tmp")
    template_temp.write_text(
        yaml.safe_dump(
            {
                "voice_id": name,
                "ref_audio": ref_audio,
                "ref_text": artifact.text,
                "gpt_model": None,
                "sovits_model": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "instruct": artifact.instruct,
                "model": artifact.model,
            },
            allow_unicode=True,
            sort_keys=False,
            width=120,
        ),
        encoding="utf-8",
    )
    template_temp.replace(template_path)
    return ref_audio


def _reload_voices(reloader: GsvVoiceReloader) -> tuple[bool, str | None]:
    """Ask GSV to re-read its registry. Never fatal.

    The files are already on disk and GSV loads them at next start, so a dead
    sidecar degrades to "takes effect later" rather than losing the freeze.

    The reloader is passed in rather than reached for as a module global: it is
    built inside ``create_tts_lab_app`` (it owns the app's HTTP client and is
    closed by the app's shutdown handler), and both routes that write voices
    share this one implementation so they cannot disagree about what a failed
    reload means.
    """

    try:
        reloader.reload()
        return True, None
    except Exception as exc:
        logger.warning("GSV voice reload failed: %s", exc)
        return False, str(exc) or exc.__class__.__name__


def _references_template(voice_path: Path, template_name: str) -> bool:
    """Best-effort read: a broken sibling must not break someone else's freeze."""

    try:
        document = yaml.safe_load(voice_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(document, dict) and str(document.get("template") or "").strip() == template_name


class GsvVoiceReloader:
    """Asks a running GSV sidecar to re-read persona voice profiles.

    The reload route may be missing on older sidecars and the sidecar itself may
    be down; freeze treats every failure as non-fatal because the profiles are
    already on disk and GSV picks them up at its next start.
    """

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or os.getenv("GSV_TTS_BASE_URL", "http://127.0.0.1:9014")).rstrip("/")
        self.client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def reload(self) -> None:
        url = f"{self.base_url}/v1/voices/reload"
        response = self.client.post(url, timeout=10.0)
        if response.status_code >= 400:
            # Carry the sidecar's own sentence through. It names the template
            # that would not parse -- the one fact the operator needs and the
            # only one this process cannot reconstruct from a status code. The
            # bare "HTTP 400" shipped once and sent someone restarting a
            # sidecar that had already answered.
            raise RuntimeError(
                f"GSV voice reload failed with HTTP {response.status_code} at {url}: "
                f"{_response_detail(response)}"
            )


def _response_detail(response) -> str:
    """The sidecar's ``detail`` when it sent one, else its body, else the code."""

    try:
        detail = response.json().get("detail")
    except Exception:
        detail = None
    return str(detail or response.text or "").strip() or "no response body"


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


class VoiceDesignRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    language: str = Field(default="Chinese", min_length=1, max_length=32)
    instruct: str = Field(min_length=1, max_length=4000)
    max_new_tokens: int = Field(default=2048, ge=128, le=4096)


class VoiceDesignPolishRequest(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    language: str = Field(default="Chinese", min_length=1, max_length=32)


class VoiceDesignFreezeRequest(BaseModel):
    """Freeze references an audition by token only.

    Accepting text/audio here would let the frozen ref_text drift from the audio
    it describes, so extra fields are rejected outright.
    """

    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)


class VoiceDesignSaveTemplateRequest(BaseModel):
    """Save-as-template names the audition and the name to file it under.

    Same reasoning as :class:`VoiceDesignFreezeRequest`: the transcript comes
    from the audition, never from the client, so it cannot drift from the audio
    it describes.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)


def create_tts_lab_app(
    runtime: TtsLabRuntime | None = None,
    *,
    voice_design: Qwen3VoiceDesignSidecar | None = None,
    config_path: str = "config.yaml",
    model_factory=None,
    voice_reloader: GsvVoiceReloader | None = None,
    settings_factory=None,
):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, Response
    except ImportError as exc:
        raise RuntimeError("TTS Lab requires the api extra") from exc

    lab = runtime or TtsLabRuntime()
    voice_design_tool = voice_design or Qwen3VoiceDesignSidecar(
        os.getenv("CHARACTER_TTS_QWEN3_VOICE_DESIGN_BASE", "http://127.0.0.1:9015")
    )
    polish_model_holder: dict[str, object] = {}
    polish_model_lock = threading.Lock()
    voice_reloader_tool = voice_reloader or GsvVoiceReloader()
    voice_artifacts = VoiceDesignArtifactStore()

    def get_settings():
        if settings_factory is not None:
            return settings_factory()
        from character_memory.config import load_settings

        return load_settings(config_path)

    def get_character_profiles() -> list[dict[str, str]]:
        from character_memory.config import discover_character_profiles

        return discover_character_profiles(get_settings())

    def get_polish_model():
        model = polish_model_holder.get("model")
        if model is not None:
            return model
        with polish_model_lock:
            model = polish_model_holder.get("model")
            if model is None:
                if model_factory is not None:
                    model = model_factory()
                else:
                    from character_memory.app import build_model
                    from character_memory.config import load_settings

                    model = build_model(load_settings(config_path))
                polish_model_holder["model"] = model
            return model

    web_dir = Path(__file__).with_name("web")
    app = FastAPI(title="Character Memory TTS Provider Lab", version="0.1")
    attach_static_assets(app, web_dir)

    @app.on_event("shutdown")
    def shutdown():
        lab.close()
        voice_design_tool.close()
        voice_reloader_tool.close()
        model = polish_model_holder.get("model")
        if model is not None:
            close = getattr(model, "close", None)
            if callable(close):
                close()

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

    @app.get("/v1/characters")
    def characters():
        # The Voice Design panel needs to name a target character for freezing.
        # The browser talks to :9002 directly, so the Lab needs its own copy of
        # the list rather than reaching across to :8001. ``persona_path`` is a
        # local filesystem path and is deliberately dropped: characters are
        # addressed by id, and the freeze route re-derives the directory itself.
        return {
            "characters": [
                {"id": item["id"], "name": item.get("name") or item["id"]}
                for item in get_character_profiles()
            ]
        }

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

    @app.get("/v1/voice-design/status")
    def voice_design_status():
        return {"voice_design": voice_design_tool.status()}

    @app.post("/v1/voice-design/polish")
    def voice_design_polish(req: VoiceDesignPolishRequest):
        started = time.perf_counter()
        try:
            model = get_polish_model()
            complete = getattr(model, "complete_text_for_session", None)
            if not callable(complete):
                raise RuntimeError("configured model does not expose the public text-completion contract")
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是 TTS 声线描述编辑器。把用户的自然语言要求整理成适合 "
                        "Qwen3-TTS VoiceDesign 的 instruct。保留用户指定的年龄感、性别感、"
                        "音高、音色、语速、口音、情绪和表达方式；不要添加人物身份、台词内容"
                        "或用户没有要求的设定。只输出最终 instruct，不要解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"目标语种：{req.language}\n原始声线描述：{req.description.strip()}",
                },
            ]
            reply = str(complete(messages, "tts-voice-design-polish") or "").strip()
            if not reply:
                raise RuntimeError("LLM returned an empty VoiceDesign instruction")
            return {
                "ok": True,
                "instruct": reply,
                "model": str(getattr(model, "model", "")),
                "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"VoiceDesign prompt polish failed: {exc}") from exc

    @app.post("/v1/voice-design/generate")
    def voice_design_generate(req: VoiceDesignRequest):
        status = voice_design_tool.status()
        if not status.get("ready"):
            raise HTTPException(
                status_code=503,
                detail=status.get("reason") or "Qwen3 VoiceDesign sidecar is not ready",
            )
        try:
            result = voice_design_tool.generate(
                req.text,
                language=req.language,
                instruct=req.instruct,
                max_new_tokens=req.max_new_tokens,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        artifact_token = voice_artifacts.put(
            VoiceDesignArtifact(
                audio=result.audio,
                text=req.text,
                language=req.language,
                instruct=req.instruct,
                sample_rate=result.sample_rate,
                model=result.model,
            )
        )
        return Response(
            content=result.audio,
            media_type=result.media_type,
            headers={
                "X-Voice-Design-Artifact": artifact_token,
                "X-Voice-Design-Model": quote(result.model, safe="/:._-"),
                "X-Voice-Design-Device": result.device,
                "X-Voice-Design-Inference-Ms": str(result.inference_ms),
                "X-Voice-Design-Audio-Ms": str(result.audio_ms),
                "X-Voice-Design-Sample-Rate": str(result.sample_rate),
            },
        )

    @app.post("/v1/voice-design/freeze")
    def voice_design_freeze(req: VoiceDesignFreezeRequest):
        profile = next(
            (item for item in get_character_profiles() if item["id"] == req.character_id),
            None,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {req.character_id}")

        artifact = voice_artifacts.get(req.artifact_id)
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail="This auditioned voice is no longer available; generate it again before freezing.",
            )

        character_id = profile["id"]
        # The directory comes from the discovered profile, never from the
        # client-supplied id, so a request cannot address a path outside the
        # persona tree.
        persona_dir = Path(profile["persona_path"]).parent
        voices_root = template_root(None)
        template_name = _template_name_for(character_id, persona_dir)
        # WAV and template first, the reference last. This order means a crash
        # leaves an unreferenced file, never a reference to a template with no
        # audio. The helper writes the first two; the reference is this route's
        # own, because only the route knows which character is being pointed.
        ref_audio = _write_voice_template(voices_root, template_name, artifact)

        # Who else is already using this template? Overwriting changes their
        # voice too, so the caller needs to be able to warn before it happens.
        shared_with = sorted(
            other["id"]
            for other in get_character_profiles()
            if other["id"] != character_id
            and _references_template(
                Path(other["persona_path"]).parent / "voice.yaml", template_name
            )
        )

        reference_path = persona_dir / "voice.yaml"
        reference_temp = reference_path.with_suffix(".yaml.tmp")
        reference_temp.write_text(
            yaml.safe_dump({"template": template_name}, sort_keys=False), encoding="utf-8"
        )
        reference_temp.replace(reference_path)

        activated, reason = _reload_voices(voice_reloader_tool)

        return {
            "ok": True,
            "character_id": character_id,
            "voice_id": template_name,
            "template": template_name,
            "ref_audio": ref_audio,
            "ref_text": artifact.text,
            "shared_with": shared_with,
            "activated": activated,
            "reason": reason,
        }

    @app.post("/v1/voice-design/save-template")
    def voice_design_save_template(req: VoiceDesignSaveTemplateRequest):
        """Save an auditioned voice as a reusable template, with no character involved."""

        try:
            name = validate_template_name(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        artifact = voice_artifacts.get(req.artifact_id)
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail="This auditioned voice is no longer available; generate it again before saving.",
            )

        voices_root = template_root(None)
        template_path = voices_root / f"{name}.yaml"
        if template_path.exists():
            # A name the user typed colliding is a mistake, not an intended
            # overwrite. Re-freezing a character is the deliberate overwrite path.
            raise HTTPException(
                status_code=409,
                detail=f"模板 {name} 已存在；换个名字，或直接固化到角色以覆盖。",
            )

        # Same writer the freeze route uses: one place decides how a template is
        # persisted, so the two paths cannot drift into two shapes.
        ref_audio = _write_voice_template(voices_root, name, artifact)

        activated, reason = _reload_voices(voice_reloader_tool)

        return {
            "ok": True,
            "template": name,
            "ref_audio": ref_audio,
            "ref_text": artifact.text,
            "activated": activated,
            "reason": reason,
        }

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("TTS Lab requires the api extra") from exc
    host = os.getenv("CHARACTER_TTS_LAB_HOST", "127.0.0.1")
    port = int(os.getenv("CHARACTER_TTS_LAB_PORT", "9002"))
    print(f"tts-lab: http://{host}:{port}/tts")
    config_path = os.getenv("CHARACTER_CONFIG_PATH", os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml"))
    uvicorn.run(create_tts_lab_app(config_path=config_path), host=host, port=port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
