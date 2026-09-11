from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading
import time
from typing import Sequence


_MEDIA_SUFFIXES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider: str
    model: str
    device: str
    transcription_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


class SpeechRecognitionProvider(ABC):
    """Replaceable local/remote ASR boundary.

    Providers return draft text only. They never own chat submission.
    """

    @abstractmethod
    def transcribe(
        self,
        audio: bytes,
        *,
        media_type: str,
        language: str,
        hotwords: Sequence[str] = (),
    ) -> TranscriptionResult:
        raise NotImplementedError


class FunASRProvider(SpeechRecognitionProvider):
    """Lazy local GPU provider backed by Fun-ASR-Nano via FunASR AutoModel."""

    def __init__(
        self,
        *,
        model: str = "FunAudioLLM/Fun-ASR-Nano-2512",
        device: str = "cuda:0",
        hub: str = "ms",
    ):
        self.model_id = model
        self.device = device
        self.hub = hub
        self._model = None
        self._lock = threading.RLock()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError(
                'FunASR 未安装。请先安装 GPU 版 PyTorch/torchaudio，再执行 pip install -e ".[asr]"'
            ) from exc

        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch 未安装，FunASR GPU 推理不可用") from exc

        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                f"配置了 {self.device}，但 torch.cuda.is_available() 为 False；请安装与 NVIDIA 驱动匹配的 GPU PyTorch"
            )

        self._model = AutoModel(
            model=self.model_id,
            device=self.device,
            hub=self.hub,
            disable_update=True,
        )
        return self._model

    @staticmethod
    def _suffix_for(media_type: str) -> str:
        normalized = (media_type or "").split(";", 1)[0].strip().lower()
        return _MEDIA_SUFFIXES.get(normalized, ".webm")

    @staticmethod
    def _extract_text(result) -> str:
        if not result:
            return ""
        first = result[0] if isinstance(result, list) else result
        if isinstance(first, dict):
            return str(first.get("text") or "").strip()
        return str(first).strip()

    def transcribe(
        self,
        audio: bytes,
        *,
        media_type: str,
        language: str,
        hotwords: Sequence[str] = (),
    ) -> TranscriptionResult:
        if not audio:
            raise ValueError("empty audio")

        started = time.perf_counter()
        suffix = self._suffix_for(media_type)
        temp_path: Path | None = None
        with self._lock:
            model = self._ensure_model()
            try:
                with NamedTemporaryFile(prefix="character-memory-asr-", suffix=suffix, delete=False) as handle:
                    handle.write(audio)
                    temp_path = Path(handle.name)
                result = model.generate(
                    input=str(temp_path),
                    cache={},
                    batch_size=1,
                    hotwords=[str(item).strip() for item in hotwords if str(item).strip()],
                    language=language,
                    itn=True,
                )
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        text = self._extract_text(result)
        if not text:
            raise ValueError("未识别到可用文本")
        return TranscriptionResult(
            text=text,
            provider="funasr",
            model=self.model_id,
            device=self.device,
            transcription_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def create_speech_provider(settings) -> SpeechRecognitionProvider:
    provider = str(getattr(settings, "asr_provider", "funasr") or "funasr").strip().lower()
    if provider in {"funasr", "fun-asr"}:
        return FunASRProvider(
            model=str(getattr(settings, "asr_model", "FunAudioLLM/Fun-ASR-Nano-2512")),
            device=str(getattr(settings, "asr_device", "cuda:0")),
            hub=str(getattr(settings, "asr_hub", "ms")),
        )
    if provider in {"", "none", "disabled", "off"}:
        raise RuntimeError("本地语音识别已禁用")
    raise ValueError(f"unknown asr_provider: {provider}")
