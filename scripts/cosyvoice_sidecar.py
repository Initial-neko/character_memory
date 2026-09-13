from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import wave

import numpy as np


ROOT = Path(os.getenv("COSYVOICE_ROOT", ".external/CosyVoice")).resolve()
MODEL_DIR = Path(
    os.getenv(
        "COSYVOICE_MODEL_DIR",
        str(ROOT / "pretrained_models" / "CosyVoice-300M-SFT"),
    )
).resolve()
HOST = os.getenv("COSYVOICE_SIDECAR_HOST", "127.0.0.1")
PORT = int(os.getenv("COSYVOICE_SIDECAR_PORT", "9012"))
DEFAULT_VOICE = os.getenv("COSYVOICE_DEFAULT_VOICE", "中文女")


class CosyVoiceEngine:
    def __init__(self):
        self.model = None
        self.lock = threading.RLock()
        self.voices: list[str] = [DEFAULT_VOICE]

    def _ensure(self):
        if self.model is not None:
            return self.model
        with self.lock:
            if self.model is not None:
                return self.model
            if not ROOT.is_dir():
                raise RuntimeError(f"COSYVOICE_ROOT not found: {ROOT}")
            if not MODEL_DIR.is_dir():
                raise RuntimeError(f"COSYVOICE_MODEL_DIR not found: {MODEL_DIR}")
            matcha = ROOT / "third_party" / "Matcha-TTS"
            for value in (ROOT, matcha):
                text = str(value)
                if text not in sys.path:
                    sys.path.insert(0, text)
            try:
                from cosyvoice.cli.cosyvoice import AutoModel
            except ImportError as exc:
                raise RuntimeError(
                    "Unable to import CosyVoice. Run this sidecar from the isolated CosyVoice Python 3.10 environment."
                ) from exc
            self.model = AutoModel(model_dir=str(MODEL_DIR))
            list_spks = getattr(self.model, "list_available_spks", None)
            if callable(list_spks):
                values = [str(value) for value in (list_spks() or [])]
                if values:
                    self.voices = values
            return self.model

    def status(self) -> dict:
        reason = None
        ready = ROOT.is_dir() and MODEL_DIR.is_dir()
        if not ROOT.is_dir():
            reason = f"COSYVOICE_ROOT not found: {ROOT}"
        elif not MODEL_DIR.is_dir():
            reason = f"COSYVOICE_MODEL_DIR not found: {MODEL_DIR}"
        device = "unknown"
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            pass
        return {
            "ok": True,
            "ready": ready,
            "loaded": self.model is not None,
            "model": str(MODEL_DIR),
            "device": device,
            "voices": self.voices,
            "default_voice": DEFAULT_VOICE if DEFAULT_VOICE in self.voices else self.voices[0],
            "reason": reason,
        }

    @staticmethod
    def _to_numpy(value) -> np.ndarray:
        if hasattr(value, "detach"):
            value = value.detach()
        if hasattr(value, "cpu"):
            value = value.cpu()
        if hasattr(value, "numpy"):
            value = value.numpy()
        return np.asarray(value, dtype=np.float32).reshape(-1)

    @staticmethod
    def _wav(samples: np.ndarray, sample_rate: int) -> bytes:
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm.tobytes())
        return out.getvalue()

    def synthesize(self, text: str, voice: str) -> tuple[bytes, dict]:
        value = str(text or "").strip()
        if not value:
            raise ValueError("empty TTS text")
        model = self._ensure()
        selected_voice = str(voice or self.voices[0])
        started = time.perf_counter()
        chunks: list[np.ndarray] = []
        with self.lock:
            for item in model.inference_sft(value, selected_voice, stream=False):
                speech = item.get("tts_speech") if isinstance(item, dict) else None
                if speech is not None:
                    audio = self._to_numpy(speech)
                    if audio.size:
                        chunks.append(audio)
        if not chunks:
            raise RuntimeError("CosyVoice returned no audio")
        samples = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        sample_rate = int(getattr(model, "sample_rate", 22050))
        inference_ms = (time.perf_counter() - started) * 1000.0
        audio_ms = len(samples) * 1000.0 / max(1, sample_rate)
        return self._wav(samples, sample_rate), {
            "voice": selected_voice,
            "sample_rate": sample_rate,
            "inference_ms": round(inference_ms, 1),
            "audio_ms": round(audio_ms, 1),
        }


ENGINE = CosyVoiceEngine()


class Handler(BaseHTTPRequestHandler):
    server_version = "CharacterCosyVoiceSidecar/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"cosyvoice-sidecar: {fmt % args}", flush=True)

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"detail": "not found"})
            return
        self._json(200, ENGINE.status())

    def do_POST(self) -> None:
        if self.path != "/v1/tts":
            self._json(404, {"detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 128 * 1024:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            audio, meta = ENGINE.synthesize(body.get("text", ""), body.get("voice", DEFAULT_VOICE))
            status = ENGINE.status()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("X-TTS-Inference-Ms", str(meta["inference_ms"]))
            self.send_header("X-TTS-Audio-Ms", str(meta["audio_ms"]))
            self.send_header("X-TTS-Sample-Rate", str(meta["sample_rate"]))
            self.send_header("X-TTS-Model", "CosyVoice-300M-SFT")
            self.send_header("X-TTS-Device", str(status.get("device") or "unknown"))
            self.end_headers()
            self.wfile.write(audio)
        except ValueError as exc:
            self._json(400, {"detail": str(exc)})
        except Exception as exc:
            self._json(503, {"detail": str(exc)})


def main() -> None:
    print(f"cosyvoice-sidecar: root={ROOT}", flush=True)
    print(f"cosyvoice-sidecar: model={MODEL_DIR}", flush=True)
    print(f"cosyvoice-sidecar: http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
