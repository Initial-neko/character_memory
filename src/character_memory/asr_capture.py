"""Capture what the ASR path actually received, so a change can be judged.

This is a measuring instrument, not a feature. The endpoint, the capture policy
and the recognizer all change what text comes back, and until the audio the model
really saw sits next to the text it produced, there is no way to tell a capture
loss from a decode loss -- the two need opposite fixes. So the Media Runtime,
which is the process that receives the WAV, records the pair while a test run has
it switched on. Nothing is written when it is off, which is the default.

The record is deliberately the *raw upload*, not a re-encoded copy: it is exactly
the model's input, so a bad transcript can be compared against the bytes that
caused it. That also keeps the browser out of the loop entirely -- the call and
dictation paths stay byte-for-byte what they were, and whatever they produce is
what gets measured.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

_INDEX_NAME = "index.jsonl"


@dataclass(frozen=True)
class CaptureRecord:
    """One transcription as it happened, plus the audio it was made from."""

    id: str
    at: str
    at_epoch: float
    source: str
    text: str
    provider: str
    model: str
    device: str
    audio_ms: float
    inference_ms: float
    bytes: int
    # Reserved for the speech-detection step: once endpointing moves server-side
    # this carries which segments the gate found, so "the model hallucinated on
    # noise" and "the gate let noise through" stay distinguishable in old records.
    vad: dict[str, Any] | None = None


def _new_id(epoch: float) -> str:
    # Sortable prefix first so the directory listing reads in capture order even
    # without the index, and a short random tail so two captures in the same
    # millisecond cannot collide.
    return f"{int(epoch * 1000)}-{uuid.uuid4().hex[:8]}"


class AsrCaptureStore:
    """Append-only capture directory with a bounded, self-trimming footprint.

    Recording is off until a test run turns it on, and the switch lives in memory
    only: a restart returns to off. A capture is runtime data about a person's
    voice, so "off" has to be the state that survives anything going wrong.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self._max_items = max(1, int(max_items))
        self._max_bytes = max(1, int(max_bytes))
        self._enabled = False
        # Sync handlers run in FastAPI's threadpool, so records arrive from more
        # than one thread; the lock covers the write, the index append and the
        # trim as one step, or a trim can delete a file another thread just wrote.
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled = bool(enabled)
            return self._enabled

    def record(
        self,
        *,
        wav: bytes,
        result: Any,
        source: str = "unknown",
        vad: dict[str, Any] | None = None,
    ) -> CaptureRecord | None:
        """Store one upload with its transcript. Returns None when switched off."""
        with self._lock:
            if not self._enabled:
                return None
            epoch = time.time()
            record = CaptureRecord(
                id=_new_id(epoch),
                at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)),
                at_epoch=epoch,
                source=str(source or "unknown"),
                text=str(getattr(result, "text", "") or ""),
                provider=str(getattr(result, "provider", "") or ""),
                model=str(getattr(result, "model", "") or ""),
                device=str(getattr(result, "device", "") or ""),
                audio_ms=float(getattr(result, "audio_ms", 0.0) or 0.0),
                inference_ms=float(getattr(result, "inference_ms", 0.0) or 0.0),
                bytes=len(wav),
                vad=vad,
            )
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / f"{record.id}.wav").write_bytes(wav)
            with (self.directory / _INDEX_NAME).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            self._trim_locked()
            return record

    def list(self, limit: int = 100) -> list[dict]:
        """Newest first. Records whose audio is gone are skipped, not returned."""
        with self._lock:
            records = self._read_locked()
        present = [item for item in records if (self.directory / f"{item['id']}.wav").is_file()]
        present.sort(key=lambda item: item.get("at_epoch", 0.0), reverse=True)
        return present[: max(1, int(limit))]

    def audio_path(self, item_id: str) -> Path | None:
        """Resolve an id to its file, refusing anything that is not a plain id."""
        candidate = str(item_id or "")
        if not candidate or candidate != Path(candidate).name or candidate.startswith("."):
            return None
        path = self.directory / f"{candidate}.wav"
        if not path.is_file():
            return None
        if path.parent.resolve() != self.directory.resolve():
            return None
        return path

    def clear(self) -> int:
        """Delete every capture. Returns how many records were dropped."""
        with self._lock:
            dropped = len(self._read_locked())
            if self.directory.is_dir():
                for path in self.directory.glob("*.wav"):
                    path.unlink(missing_ok=True)
                (self.directory / _INDEX_NAME).unlink(missing_ok=True)
            return dropped

    def _read_locked(self) -> list[dict]:
        index = self.directory / _INDEX_NAME
        if not index.is_file():
            return []
        records: list[dict] = []
        with index.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    # A half-written trailing line is not worth failing a listing
                    # over; the records before it are still good.
                    continue
                if isinstance(parsed, dict) and parsed.get("id"):
                    records.append(parsed)
        return records

    def _trim_locked(self) -> None:
        """Drop the oldest captures until both caps hold."""
        records = self._read_locked()
        if len(records) <= self._max_items and sum(int(r.get("bytes", 0)) for r in records) <= self._max_bytes:
            return
        records.sort(key=lambda item: item.get("at_epoch", 0.0))
        # Walk oldest-first, dropping until the remainder fits under both caps.
        kept: list[dict] = []
        total = 0
        for item in reversed(records):
            size = int(item.get("bytes", 0))
            if len(kept) >= self._max_items or total + size > self._max_bytes:
                (self.directory / f"{item['id']}.wav").unlink(missing_ok=True)
                continue
            kept.append(item)
            total += size
        kept.reverse()
        with (self.directory / _INDEX_NAME).open("w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
