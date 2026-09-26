"""Unified ASR session primitives.

Phase 1 compatibility layer for the streaming ASR migration. It does not
pretend that today's offline provider is streaming. Audio is accumulated
losslessly per segment and the existing provider is invoked only at flush.

The public session identifiers and transcript event contract are intended to
remain stable when a real streaming provider is introduced.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import List, Optional, Protocol
import uuid

import numpy as np


class AsrSessionState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    FINALIZING = "finalizing"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TranscriptKind(str, Enum):
    PARTIAL = "partial"
    FINAL = "final"


class EndpointReason(str, Enum):
    SILENCE = "silence"
    USER_STOP = "user_stop"
    FORCED_MAX_DURATION = "forced_max_duration"
    SESSION_END = "session_end"
    ERROR = "error"
    MANUAL_FLUSH = "manual_flush"


@dataclass(frozen=True)
class AsrTranscriptEvent:
    session_id: str
    segment_id: int
    revision: int
    kind: TranscriptKind
    text: str
    start_ms: float
    end_ms: float
    provider: str = ""
    inference_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "revision": self.revision,
            "kind": self.kind.value,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "provider": self.provider,
            "inference_ms": self.inference_ms,
        }


class BatchSpeechRecognitionProvider(Protocol):
    def transcribe(self, samples: np.ndarray, sample_rate: int):
        ...


@dataclass(frozen=True)
class AsrSegmentTrace:
    session_id: str
    segment_id: int
    source: str
    sample_rate: int
    audio_start_ms: float
    audio_end_ms: float
    audio_samples: int
    endpoint_reason: str
    final_text: str
    provider: str
    inference_ms: float

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "source": self.source,
            "sample_rate": self.sample_rate,
            "audio_start_ms": self.audio_start_ms,
            "audio_end_ms": self.audio_end_ms,
            "audio_samples": self.audio_samples,
            "audio_duration_ms": round(
                max(0.0, self.audio_end_ms - self.audio_start_ms), 1
            ),
            "endpoint_reason": self.endpoint_reason,
            "final_text": self.final_text,
            "provider": self.provider,
            "inference_ms": self.inference_ms,
        }


class TranscriptReconciler:
    """Keeps one authoritative transcript for one segment.

    Partial results are replaceable UI state. Final is terminal and idempotent.
    This class never creates chat messages.
    """

    def __init__(self, session_id: str, segment_id: int):
        self.session_id = session_id
        self.segment_id = segment_id
        self._revision = 0
        self._text = ""
        self._final = False

    @property
    def text(self) -> str:
        return self._text

    @property
    def final(self) -> bool:
        return self._final

    @property
    def revision(self) -> int:
        return self._revision

    def apply(
        self,
        *,
        kind: TranscriptKind,
        text: str,
        start_ms: float,
        end_ms: float,
        provider: str = "",
        inference_ms: float = 0.0,
    ) -> AsrTranscriptEvent:
        value = str(text or "").strip()
        if not value:
            raise ValueError("transcript text cannot be empty")

        if self._final:
            if kind is not TranscriptKind.FINAL:
                raise ValueError("cannot apply partial after final")
            return AsrTranscriptEvent(
                self.session_id,
                self.segment_id,
                self._revision,
                TranscriptKind.FINAL,
                self._text,
                start_ms,
                end_ms,
                provider,
                inference_ms,
            )

        self._revision += 1
        self._text = value
        if kind is TranscriptKind.FINAL:
            self._final = True
        return AsrTranscriptEvent(
            self.session_id,
            self.segment_id,
            self._revision,
            kind,
            value,
            start_ms,
            end_ms,
            provider,
            inference_ms,
        )


class AsrSession:
    """Lossless session adapter around the current batch ASR provider.

    No fake partials are generated. VAD and endpointing remain outside this
    class. Callers explicitly flush a segment when their endpoint policy says
    the utterance ended.
    """

    def __init__(
        self,
        provider: BatchSpeechRecognitionProvider,
        *,
        source: str,
        sample_rate: int = 16000,
        session_id: Optional[str] = None,
        max_segment_ms: int = 12000,
    ):
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if max_segment_ms <= 0:
            raise ValueError("max_segment_ms must be positive")
        self.provider = provider
        self.source = str(source or "unknown")
        self.sample_rate = int(sample_rate)
        self.session_id = session_id or str(uuid.uuid4())
        self.max_segment_ms = int(max_segment_ms)
        self.state = AsrSessionState.NEW
        self._next_segment_id = 1
        self._active_segment: Optional[int] = None
        self._chunks: List[np.ndarray] = []
        self._chunk_sequence = -1
        self._received_samples = 0
        self._segment_start_ms = 0.0
        self._last_audio_ms = 0.0
        self._reconciler: Optional[TranscriptReconciler] = None
        self._traces: List[AsrSegmentTrace] = []
        self._lock = threading.RLock()

    def start(self) -> str:
        with self._lock:
            if self.state is not AsrSessionState.NEW:
                raise RuntimeError("session cannot be started twice")
            self.state = AsrSessionState.ACTIVE
            return self.session_id

    def push_audio(
        self,
        samples: np.ndarray,
        *,
        sequence: int,
        start_ms: Optional[float] = None,
    ) -> Optional[str]:
        with self._lock:
            if self.state is not AsrSessionState.ACTIVE:
                raise RuntimeError("session is not active")
            if sequence <= self._chunk_sequence:
                raise ValueError(
                    "audio sequence must increase: "
                    + str(sequence)
                    + " after "
                    + str(self._chunk_sequence)
                )

            values = np.asarray(samples, dtype=np.float32).reshape(-1)
            if values.size == 0:
                raise ValueError("audio chunk cannot be empty")

            if self._active_segment is None:
                self._active_segment = self._next_segment_id
                self._next_segment_id += 1
                self._reconciler = TranscriptReconciler(
                    self.session_id, self._active_segment
                )
                self._segment_start_ms = (
                    float(start_ms) if start_ms is not None else self._last_audio_ms
                )

            self._chunks.append(np.ascontiguousarray(values))
            self._chunk_sequence = int(sequence)
            self._received_samples += int(values.size)
            self._last_audio_ms = self._segment_start_ms + (
                self._received_samples * 1000.0 / self.sample_rate
            )

            if self._last_audio_ms - self._segment_start_ms >= self.max_segment_ms:
                return EndpointReason.FORCED_MAX_DURATION.value
            return None

    def flush_segment(self, reason: EndpointReason | str) -> AsrTranscriptEvent:
        with self._lock:
            if self.state is not AsrSessionState.ACTIVE:
                raise RuntimeError("session is not active")
            if self._active_segment is None or not self._chunks or self._reconciler is None:
                raise ValueError("no active audio segment")

            self.state = AsrSessionState.FINALIZING
            samples = np.concatenate(self._chunks).astype(np.float32, copy=False)
            started = time.perf_counter()
            try:
                result = self.provider.transcribe(samples, self.sample_rate)
                text = str(getattr(result, "text", "") or "").strip()
                if not text:
                    raise ValueError("ASR returned empty text")
                event = self._reconciler.apply(
                    kind=TranscriptKind.FINAL,
                    text=text,
                    start_ms=self._segment_start_ms,
                    end_ms=self._last_audio_ms,
                    provider=str(getattr(result, "provider", "") or ""),
                    inference_ms=float(getattr(result, "inference_ms", 0.0) or 0.0),
                )
            except Exception:
                self.state = AsrSessionState.ACTIVE
                raise

            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            if event.inference_ms <= 0:
                event = AsrTranscriptEvent(
                    event.session_id,
                    event.segment_id,
                    event.revision,
                    event.kind,
                    event.text,
                    event.start_ms,
                    event.end_ms,
                    event.provider,
                    elapsed_ms,
                )
            self._traces.append(
                AsrSegmentTrace(
                    session_id=event.session_id,
                    segment_id=event.segment_id,
                    source=self.source,
                    sample_rate=self.sample_rate,
                    audio_start_ms=event.start_ms,
                    audio_end_ms=event.end_ms,
                    audio_samples=int(samples.size),
                    endpoint_reason=reason.value if isinstance(reason, EndpointReason) else str(reason),
                    final_text=event.text,
                    provider=event.provider,
                    inference_ms=event.inference_ms,
                )
            )
            self._reset_segment()
            self.state = AsrSessionState.ACTIVE
            return event

    def close(self) -> None:
        with self._lock:
            if self._active_segment is not None:
                raise RuntimeError("active segment must be flushed or cancelled")
            self.state = AsrSessionState.CLOSED

    def cancel(self) -> None:
        with self._lock:
            self._reset_segment()
            self.state = AsrSessionState.CANCELLED

    def active_segment_id(self) -> Optional[int]:
        with self._lock:
            return self._active_segment

    def recent_traces(self, limit: int = 50) -> list[dict]:
        with self._lock:
            count = max(1, min(int(limit), 200))
            return [item.to_dict() for item in self._traces[-count:]]

    def diagnostic_snapshot(self) -> dict:
        with self._lock:
            return {
                "session_id": self.session_id,
                "source": self.source,
                "state": self.state.value,
                "sample_rate": self.sample_rate,
                "active_segment_id": self._active_segment,
                "audio_samples": self._received_samples,
                "audio_duration_ms": round(
                    self._received_samples * 1000.0 / self.sample_rate, 1
                ),
                "last_audio_ms": round(self._last_audio_ms, 1),
                "last_sequence": self._chunk_sequence,
            }

    def _reset_segment(self) -> None:
        self._active_segment = None
        self._chunks = []
        self._received_samples = 0
        self._segment_start_ms = 0.0
        self._last_audio_ms = 0.0
        self._reconciler = None
