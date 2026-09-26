from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass
import io
import os
from pathlib import Path
import threading
import time
import wave

import numpy as np


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider: str
    model: str
    device: str
    inference_ms: float
    audio_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    sample_rate: int
    provider: str
    model: str
    device: str
    inference_ms: float
    audio_ms: float


class SpeechRecognitionProvider(ABC):
    """Local media boundary. Providers never own chat submission."""

    @abstractmethod
    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> dict:
        raise NotImplementedError


class TextToSpeechProvider(ABC):
    """Local TTS boundary. Providers turn final character text into audio only."""

    @abstractmethod
    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> dict:
        raise NotImplementedError


class UnavailableSpeechRecognitionProvider(SpeechRecognitionProvider):
    def __init__(self, reason: str):
        self.reason = reason

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        raise RuntimeError(self.reason)

    def status(self) -> dict:
        return {"ready": False, "provider": "unconfigured", "reason": self.reason}


class UnavailableTextToSpeechProvider(TextToSpeechProvider):
    def __init__(self, reason: str):
        self.reason = reason

    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        raise RuntimeError(self.reason)

    def status(self) -> dict:
        return {"ready": False, "provider": "unconfigured", "reason": self.reason}


def read_pcm16_wav(payload: bytes) -> tuple[np.ndarray, int]:
    """Decode the narrow Voice V0 WAV contract without ffmpeg/soundfile."""
    if not payload:
        raise ValueError("empty audio")
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"invalid WAV: {exc}") from exc
    if sample_width != 2:
        raise ValueError("Voice V0 accepts 16-bit PCM WAV only")
    if channels not in {1, 2}:
        raise ValueError("Voice V0 accepts mono or stereo WAV only")
    if sample_rate < 8000 or sample_rate > 96000:
        raise ValueError(f"unsupported sample rate: {sample_rate}")
    raw = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2:
        raw = raw.reshape(-1, 2).mean(axis=1)
    return np.ascontiguousarray(raw, dtype=np.float32), int(sample_rate)


def float_audio_to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.clip(values, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm.tobytes())
    return out.getvalue()


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or len(samples) == 0:
        return np.ascontiguousarray(samples, dtype=np.float32)
    target_length = max(1, int(round(len(samples) * target_rate / source_rate)))
    source_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    target_x = np.linspace(0.0, 1.0, num=target_length, endpoint=False)
    return np.interp(target_x, source_x, samples).astype(np.float32)


class SherpaSenseVoiceProvider(SpeechRecognitionProvider):
    """Small local ASR provider using sherpa-onnx SenseVoice.

    sherpa-onnx and the model are loaded lazily so the media process can start,
    expose /health, and run tests without allocating model memory.
    """

    def __init__(
        self,
        *,
        model: str,
        tokens: str,
        device: str = "cpu",
        language: str = "zh",
        num_threads: int = 2,
        use_itn: bool = True,
    ):
        self.model_path = str(model)
        self.tokens_path = str(tokens)
        self.device = str(device or "cpu")
        self.language = str(language or "zh")
        self.num_threads = max(1, int(num_threads))
        self.use_itn = bool(use_itn)
        self._recognizer = None
        self._lock = threading.RLock()

    def _ensure(self):
        if self._recognizer is not None:
            return self._recognizer
        with self._lock:
            if self._recognizer is not None:
                return self._recognizer
            for value, label in ((self.model_path, "ASR model"), (self.tokens_path, "ASR tokens")):
                if not Path(value).is_file():
                    raise RuntimeError(f"{label} not found: {value}")
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise RuntimeError('sherpa-onnx 未安装；执行 pip install -e ".[media]"') from exc
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self.model_path,
                tokens=self.tokens_path,
                num_threads=self.num_threads,
                provider=self.device,
                language=self.language,
                use_itn=self.use_itn,
            )
            return self._recognizer

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        if len(samples) == 0:
            raise ValueError("empty audio")
        recognizer = self._ensure()
        started = time.perf_counter()
        stream = recognizer.create_stream()
        stream.accept_waveform(int(sample_rate), np.asarray(samples, dtype=np.float32))
        recognizer.decode_stream(stream)
        result = stream.result
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            raise ValueError("未识别到可用文本")
        inference_ms = (time.perf_counter() - started) * 1000.0
        audio_ms = len(samples) * 1000.0 / max(1, int(sample_rate))
        return TranscriptionResult(
            text=text,
            provider="sherpa-sensevoice",
            model=self.model_path,
            device=self.device,
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )

    def status(self) -> dict:
        return {
            "ready": Path(self.model_path).is_file() and Path(self.tokens_path).is_file(),
            "loaded": self._recognizer is not None,
            "provider": "sherpa-sensevoice",
            "model": self.model_path,
            "tokens": self.tokens_path,
            "device": self.device,
            "language": self.language,
            "num_threads": self.num_threads,
            "supports_hotwords": False,
        }


class SherpaParaformerStreamingProvider(SpeechRecognitionProvider):
    """Low-resource streaming Chinese ASR using sherpa-onnx Online Paraformer.

    The provider owns model construction; each call to create_session() owns
    exactly one OnlineStream and therefore one speech session. The model is
    loaded lazily and remains resident for reuse.
    """

    def __init__(
        self,
        *,
        encoder: str,
        decoder: str,
        tokens: str,
        device: str = "cpu",
        num_threads: int = 2,
        endpoint_silence_ms: int = 1200,
        endpoint_short_silence_ms: int = 800,
    ):
        self.encoder_path = str(encoder)
        self.decoder_path = str(decoder)
        self.tokens_path = str(tokens)
        self.device = str(device or "cpu")
        self.num_threads = max(1, int(num_threads))
        self.endpoint_silence_ms = max(200, int(endpoint_silence_ms))
        self.endpoint_short_silence_ms = max(200, int(endpoint_short_silence_ms))
        self._recognizer = None
        self._lock = threading.RLock()

    def _ensure(self):
        if self._recognizer is not None:
            return self._recognizer
        with self._lock:
            if self._recognizer is not None:
                return self._recognizer
            required = (
                (self.encoder_path, "Paraformer encoder"),
                (self.decoder_path, "Paraformer decoder"),
                (self.tokens_path, "Paraformer tokens"),
            )
            for value, label in required:
                if not Path(value).is_file():
                    raise RuntimeError(f"{label} not found: {value}")
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise RuntimeError(
                    'sherpa-onnx 未安装；执行 pip install -e ".[media]"'
                ) from exc
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=self.tokens_path,
                encoder=self.encoder_path,
                decoder=self.decoder_path,
                num_threads=self.num_threads,
                sample_rate=16000,
                feature_dim=80,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=self.endpoint_silence_ms / 1000.0,
                rule2_min_trailing_silence=self.endpoint_short_silence_ms / 1000.0,
                # Do not use a short utterance-length rule as a normal cutoff.
                rule3_min_utterance_length=20.0,
                provider=self.device,
                decoding_method="greedy_search",
            )
            return self._recognizer

    def create_session(self):
        return SherpaParaformerStreamingSession(self, self._ensure())

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        if len(samples) == 0:
            raise ValueError("empty audio")
        started = time.perf_counter()
        session = self.create_session()
        session.push_audio(samples, sample_rate=sample_rate)
        result = session.finish()
        inference_ms = (time.perf_counter() - started) * 1000.0
        if not result.text:
            raise ValueError("未识别到可用文本")
        return TranscriptionResult(
            text=result.text,
            provider="sherpa-paraformer-streaming",
            model=self.encoder_path,
            device=self.device,
            inference_ms=round(inference_ms, 1),
            audio_ms=round(len(samples) * 1000.0 / max(1, int(sample_rate)), 1),
        )

    def status(self) -> dict:
        ready = all(
            Path(value).is_file()
            for value in (self.encoder_path, self.decoder_path, self.tokens_path)
        )
        return {
            "ready": ready,
            "loaded": self._recognizer is not None,
            "provider": "sherpa-paraformer-streaming",
            "model": self.encoder_path,
            "decoder": self.decoder_path,
            "tokens": self.tokens_path,
            "device": self.device,
            "num_threads": self.num_threads,
            "sample_rate": 16000,
            "streaming": True,
            "model_params": "220M-class",
            "endpoint_silence_ms": self.endpoint_silence_ms,
            "endpoint_short_silence_ms": self.endpoint_short_silence_ms,
        }


class SherpaParaformerStreamingSession:
    def __init__(self, provider: SherpaParaformerStreamingProvider, recognizer):
        self.provider = provider
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()
        self.started = time.perf_counter()
        self.audio_samples = 0
        self.closed = False
        self._last_text = ""

    def push_audio(self, samples: np.ndarray, *, sample_rate: int = 16000) -> str:
        if self.closed:
            raise RuntimeError("ASR stream is closed")
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return self._last_text
        if int(sample_rate) != 16000:
            raise ValueError("Paraformer streaming requires 16 kHz PCM audio")
        self.stream.accept_waveform(16000, values)
        self.audio_samples += int(values.size)
        self._decode_ready()
        self._last_text = str(self.recognizer.get_result(self.stream) or "").strip()
        return self._last_text

    def is_endpoint(self) -> bool:
        if self.closed:
            return True
        return bool(self.recognizer.is_endpoint(self.stream))

    def reset_endpoint(self) -> None:
        if self.closed:
            return
        self.recognizer.reset(self.stream)
        self._last_text = ""

    def finish(self):
        if self.closed:
            return type("Result", (), {"text": self._last_text})()
        # Paraformer streaming needs tail audio to flush the last look-ahead
        # window. Keep this server-side so browser endpoint timing cannot drop
        # the final syllables of an utterance.
        self.stream.accept_waveform(
            16000,
            np.zeros(int(0.6 * 16000), dtype=np.float32),
        )
        self.stream.input_finished()
        self._decode_ready(force=True)
        text = str(self.recognizer.get_result(self.stream) or "").strip()
        self.closed = True
        return type("Result", (), {"text": text})()

    def _decode_ready(self, *, force: bool = False) -> None:
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        if force:
            # input_finished() can make another decode step available.
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)

class SherpaVitsProvider(TextToSpeechProvider):
    """Small VITS TTS provider using the same sherpa-onnx runtime as ASR."""

    def __init__(
        self,
        *,
        model: str,
        tokens: str,
        lexicon: str,
        dict_dir: str = "",
        rule_fsts: str = "",
        device: str = "cpu",
        num_threads: int = 2,
    ):
        self.model_path = str(model)
        self.tokens_path = str(tokens)
        self.lexicon_path = str(lexicon)
        self.dict_dir = str(dict_dir or "")
        self.rule_fsts = str(rule_fsts or "")
        self.device = str(device or "cpu")
        self.num_threads = max(1, int(num_threads))
        self._tts = None
        self._lock = threading.RLock()

    def _ensure(self):
        if self._tts is not None:
            return self._tts
        with self._lock:
            if self._tts is not None:
                return self._tts
            required = (
                (self.model_path, "TTS model"),
                (self.tokens_path, "TTS tokens"),
                (self.lexicon_path, "TTS lexicon"),
            )
            for value, label in required:
                if not Path(value).is_file():
                    raise RuntimeError(f"{label} not found: {value}")
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise RuntimeError('sherpa-onnx 未安装；执行 pip install -e ".[media]"') from exc

            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=self.model_path,
                lexicon=self.lexicon_path,
                tokens=self.tokens_path,
                dict_dir=self.dict_dir,
            )
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                vits=vits,
                num_threads=self.num_threads,
                provider=self.device,
                debug=False,
            )
            config = sherpa_onnx.OfflineTtsConfig(
                model=model_config,
                rule_fsts=self.rule_fsts,
                max_num_sentences=1,
            )
            self._tts = sherpa_onnx.OfflineTts(config)
            return self._tts

    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        value = str(text or "").strip()
        if not value:
            raise ValueError("empty TTS text")
        tts = self._ensure()
        started = time.perf_counter()
        audio = tts.generate(value, sid=int(speaker_id), speed=float(speed))
        samples = np.asarray(audio.samples, dtype=np.float32)
        sample_rate = int(audio.sample_rate)
        inference_ms = (time.perf_counter() - started) * 1000.0
        audio_ms = len(samples) * 1000.0 / max(1, sample_rate)
        return SynthesisResult(
            audio=float_audio_to_wav(samples, sample_rate),
            sample_rate=sample_rate,
            provider="sherpa-vits",
            model=self.model_path,
            device=self.device,
            inference_ms=round(inference_ms, 1),
            audio_ms=round(audio_ms, 1),
        )

    def status(self) -> dict:
        required = [self.model_path, self.tokens_path, self.lexicon_path]
        return {
            "ready": all(Path(value).is_file() for value in required),
            "loaded": self._tts is not None,
            "provider": "sherpa-vits",
            "model": self.model_path,
            "tokens": self.tokens_path,
            "lexicon": self.lexicon_path,
            "device": self.device,
            "num_threads": self.num_threads,
        }


class MediaRuntime:
    """Owns media models in one process and keeps bounded latency traces."""

    def __init__(
        self,
        asr: SpeechRecognitionProvider,
        tts: TextToSpeechProvider,
        *,
        metrics_limit: int = 200,
    ):
        self.asr = asr
        self.tts = tts
        self.metrics = deque(maxlen=max(10, int(metrics_limit)))
        self._metrics_lock = threading.Lock()

    def record_metric(self, kind: str, **values) -> None:
        with self._metrics_lock:
            self.metrics.append({"kind": kind, "at": time.time(), **values})

    def transcribe_wav(self, payload: bytes) -> TranscriptionResult:
        samples, sample_rate = read_pcm16_wav(payload)
        started = time.perf_counter()
        result = self.asr.transcribe(samples, sample_rate)
        total_ms = round((time.perf_counter() - started) * 1000.0, 1)
        self.record_metric(
            "asr",
            inference_ms=result.inference_ms,
            total_ms=total_ms,
            audio_ms=result.audio_ms,
            provider=result.provider,
            device=result.device,
        )
        return result

    def synthesize(self, text: str, *, speaker_id: int = 0, speed: float = 1.0) -> SynthesisResult:
        started = time.perf_counter()
        result = self.tts.synthesize(text, speaker_id=speaker_id, speed=speed)
        total_ms = round((time.perf_counter() - started) * 1000.0, 1)
        self.record_metric(
            "tts",
            inference_ms=result.inference_ms,
            total_ms=total_ms,
            audio_ms=result.audio_ms,
            provider=result.provider,
            device=result.device,
        )
        return result

    def recent_metrics(self, limit: int = 50) -> list[dict]:
        with self._metrics_lock:
            return list(self.metrics)[-max(1, min(int(limit), 200)) :]

    def status(self) -> dict:
        return {
            "pid": os.getpid(),
            "asr": self.asr.status(),
            "tts": self.tts.status(),
            "metrics_buffer": len(self.metrics),
        }


def build_media_runtime_from_env() -> MediaRuntime:
    asr_provider = os.getenv("CHARACTER_MEDIA_ASR_PROVIDER", "sensevoice").strip().lower()
    if asr_provider in {"paraformer", "paraformer-streaming", "sherpa-paraformer-streaming"}:
        encoder = os.getenv("CHARACTER_MEDIA_ASR_ENCODER", "").strip()
        decoder = os.getenv("CHARACTER_MEDIA_ASR_DECODER", "").strip()
        tokens = os.getenv("CHARACTER_MEDIA_ASR_TOKENS", "").strip()
        if encoder and decoder and tokens:
            asr: SpeechRecognitionProvider = SherpaParaformerStreamingProvider(
                encoder=encoder,
                decoder=decoder,
                tokens=tokens,
                device=os.getenv("CHARACTER_MEDIA_ASR_DEVICE", "cpu"),
                num_threads=int(os.getenv("CHARACTER_MEDIA_ASR_THREADS", "2")),
                endpoint_silence_ms=int(os.getenv("CHARACTER_MEDIA_ASR_ENDPOINT_SILENCE_MS", "1200")),
                endpoint_short_silence_ms=int(os.getenv("CHARACTER_MEDIA_ASR_ENDPOINT_SHORT_SILENCE_MS", "800")),
            )
        else:
            asr = UnavailableSpeechRecognitionProvider(
                "Paraformer ASR 未配置：设置 CHARACTER_MEDIA_ASR_ENCODER / "
                "CHARACTER_MEDIA_ASR_DECODER / CHARACTER_MEDIA_ASR_TOKENS"
            )
    else:
        asr_model = os.getenv("CHARACTER_MEDIA_ASR_MODEL", "").strip()
        asr_tokens = os.getenv("CHARACTER_MEDIA_ASR_TOKENS", "").strip()
        if asr_model and asr_tokens:
            asr = SherpaSenseVoiceProvider(
                model=asr_model,
                tokens=asr_tokens,
                device=os.getenv("CHARACTER_MEDIA_ASR_DEVICE", "cpu"),
                language=os.getenv("CHARACTER_MEDIA_ASR_LANGUAGE", "zh"),
                num_threads=int(os.getenv("CHARACTER_MEDIA_ASR_THREADS", "2")),
            )
        else:
            asr = UnavailableSpeechRecognitionProvider(
                "ASR 未配置：设置 CHARACTER_MEDIA_ASR_MODEL / CHARACTER_MEDIA_ASR_TOKENS"
            )

    tts_model = os.getenv("CHARACTER_MEDIA_TTS_MODEL", "").strip()
    tts_tokens = os.getenv("CHARACTER_MEDIA_TTS_TOKENS", "").strip()
    tts_lexicon = os.getenv("CHARACTER_MEDIA_TTS_LEXICON", "").strip()
    if tts_model and tts_tokens and tts_lexicon:
        tts: TextToSpeechProvider = SherpaVitsProvider(
            model=tts_model,
            tokens=tts_tokens,
            lexicon=tts_lexicon,
            dict_dir=os.getenv("CHARACTER_MEDIA_TTS_DICT_DIR", ""),
            rule_fsts=os.getenv("CHARACTER_MEDIA_TTS_RULE_FSTS", ""),
            device=os.getenv("CHARACTER_MEDIA_TTS_DEVICE", "cpu"),
            num_threads=int(os.getenv("CHARACTER_MEDIA_TTS_THREADS", "2")),
        )
    else:
        tts = UnavailableTextToSpeechProvider(
            "TTS 未配置：设置 CHARACTER_MEDIA_TTS_MODEL / CHARACTER_MEDIA_TTS_TOKENS / CHARACTER_MEDIA_TTS_LEXICON"
        )
    return MediaRuntime(asr, tts)
