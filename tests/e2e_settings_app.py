from __future__ import annotations

import os
from pathlib import Path

from character_memory.settings_server import create_settings_app
from character_memory.settings_store import SettingsStore


class _Response:
    def __init__(self, payload=None, *, status_code=200, content=b"", headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = ""

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _RuntimeClient:
    def __init__(self):
        self.providers = {
            "kokoro": {
                "id": "kokoro",
                "label": "Kokoro 82M v1.1 zh",
                "ready": True,
                "loaded": True,
                "voices": ["zf_001", "zf_002"],
                "default_voice": "zf_001",
                "supports_speed": True,
                "device": "cpu",
                "model": "kokoro",
                "reason": None,
            },
            "sherpa": {
                "id": "sherpa",
                "label": "Sherpa VITS",
                "ready": True,
                "loaded": True,
                "voices": ["0", "2", "5"],
                "default_voice": "0",
                "supports_speed": True,
                "device": "cpu",
                "model": "sherpa",
                "reason": None,
            },
            "edge": {
                "id": "edge",
                "label": "Microsoft Edge TTS (online)",
                "ready": True,
                "loaded": True,
                "voices": ["zh-CN-XiaoxiaoNeural"],
                "default_voice": "zh-CN-XiaoxiaoNeural",
                "supports_speed": True,
                "device": "cloud",
                "model": "edge",
                "reason": None,
            },
            "gsv": {
                "id": "gsv",
                "label": "GSV-TTS-Lite",
                "ready": True,
                "loaded": False,
                "voices": ["murasame"],
                "default_voice": "murasame",
                "supports_speed": True,
                "device": "cuda:0",
                "model": "fake-gsv",
                "reason": None,
            },
        }

    def get(self, url, **kwargs):
        marker = "/v1/providers/"
        if marker in url:
            provider_id = url.rsplit(marker, 1)[1]
            return _Response({"provider": self.providers[provider_id]})
        return _Response({"ok": True, "ready": True})

    def post(self, url, **kwargs):
        if url.endswith("/v1/configure"):
            return _Response({"ready": True, "loaded": bool(kwargs.get("json", {}).get("preload"))})
        if url.endswith("/v1/load") or url.endswith("/v1/unload"):
            return _Response({"ready": True})
        if url.endswith("/v1/tts"):
            return _Response(
                content=b"RIFFpreview",
                headers={
                    "content-type": "audio/wav",
                    "x-tts-voice": "murasame",
                    "x-tts-device": "cuda:0",
                },
            )
        return _Response({"ok": True})


root = Path(os.environ["CHARACTER_SETTINGS_E2E_ROOT"])
root.mkdir(parents=True, exist_ok=True)
config = root / "config.yaml"
if not config.exists():
    config.write_text(
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n'
        'tts_speed: 1.0\n'
        'tts_device: "cpu"\n'
        'embedding_provider: "deterministic"\n'
        'embedding_model: "deterministic"\n',
        encoding="utf-8",
    )

app = create_settings_app(
    str(config),
    store=SettingsStore(str(config), str(root / ".env")),
    runtime_http_client=_RuntimeClient(),
)
