from __future__ import annotations

import io
import wave

import numpy as np


def read_pcm16_wav(payload: bytes) -> tuple[np.ndarray, int]:
    """Decode Character Memory's narrow PCM16 WAV contract."""

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
    """Encode mono float audio as clamped PCM16 WAV.

    Provider output occasionally contains NaN/Inf during a failed or unstable
    inference. Normalize those values before clipping so every TTS runtime emits
    the same deterministic PCM contract instead of maintaining subtly different
    encoders.
    """

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
