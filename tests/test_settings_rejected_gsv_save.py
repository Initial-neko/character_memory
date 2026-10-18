"""Offline evidence: rejected Settings saves must restore provisional GSV state.

Uses the real Settings route, real GSV configure route/runtime, temporary files,
an injected voice registry and a warm-engine sentinel. No GPU/model is loaded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from character_memory.gsv_tts_runtime import GsvTtsRuntime, create_gsv_tts_app
from character_memory.settings_server import create_settings_app
from character_memory.settings_store import SettingsStore
from character_memory.voices import VoiceProfile


class RejectedSaveLeftRuntimeChanged(AssertionError):
    """Only this verified product invariant may count as expected failure."""


@pytest.mark.parametrize(("rejection", "fail_restore"), [
    ("invalid_voice", False),
    ("unhealthy_provider", False),
    ("invalid_voice", True),
])
def test_rejected_save_restores_provisional_gsv_runtime(tmp_path, monkeypatch, rejection, fail_restore):
    for name in ("GSV_TTS_GPT_MODEL", "GSV_TTS_SOVITS_MODEL", "GSV_TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)
    old_gpt, new_gpt, sovits, ref = [tmp_path / name for name in
                                    ("old.ckpt", "new.ckpt", "shared.pth", "ref.wav")]
    for asset in (old_gpt, new_gpt, sovits, ref):
        asset.write_bytes(b"offline-contract-fixture")
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\ntts_voice: "murasame"\ntts_device: "cuda"\n', encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(f"GSV_TTS_GPT_MODEL={json.dumps(str(old_gpt))}\n"
                   f"GSV_TTS_SOVITS_MODEL={json.dumps(str(sovits))}\n"
                   'GSV_TTS_VOICE="murasame"\n', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (config, env)}
    factory_calls = []

    def forbidden_factory(*args, **kwargs):
        factory_calls.append(True)
        raise AssertionError("No model may be loaded by this offline rejection test")

    engine = GsvTtsRuntime(gpt_model=str(old_gpt), sovits_model=str(sovits),
                           device="cuda", voices={"murasame": VoiceProfile(
                               voice_id="murasame", ref_audio=str(ref), ref_text="样例")},
                           tts_factory=forbidden_factory,
                           persona_root=str(tmp_path / "personas"),
                           voices_root=str(tmp_path / "voices"))
    warm_sentinel = object()
    engine._tts = warm_sentinel
    assert engine.status()["ready"] is True
    assert engine.status()["loaded"] is True
    requested_gpt = new_gpt if rejection == "invalid_voice" else tmp_path / "missing.ckpt"
    requested = {"GSV_TTS_GPT_MODEL": str(requested_gpt), "tts_voice": "missing-voice"}
    calls = []

    with TestClient(create_gsv_tts_app(runtime=engine)) as sidecar:
        class OfflineRuntimeClient:
            def get(self, url, **kwargs):
                provider = url.rsplit("/", 1)[-1]
                status = engine.status() if provider == "gsv" else {
                    "id": provider, "ready": False, "voices": [], "reason": "offline fixture"}
                return httpx.Response(200, json={"provider": status}, request=httpx.Request("GET", url))

            def post(self, url, **kwargs):
                calls.append({"path": urlsplit(url).path, "json": kwargs.get("json")})
                if fail_restore and len(calls) == 2:
                    raise httpx.ConnectError("restore unavailable",
                                             request=httpx.Request("POST", url))
                reply = sidecar.post(urlsplit(url).path, json=kwargs.get("json"))
                return httpx.Response(reply.status_code, content=reply.content,
                                      request=httpx.Request("POST", url))

        app = create_settings_app(str(config), store=SettingsStore(str(config), str(env)),
                                  runtime_http_client=OfflineRuntimeClient())
        with TestClient(app) as client:
            response = client.patch("/v1/settings", json={"values": requested})
            durable = client.get("/v1/settings").json()["values"]

    if fail_restore:
        assert response.status_code == 500, response.text
        assert "恢复 GSV 运行态失败" in response.json()["detail"]
        assert "restore unavailable" in response.json()["detail"]
    else:
        assert response.status_code == 400, response.text
        if rejection == "invalid_voice":
            assert "missing-voice" in response.json()["detail"]
        else:
            assert "health check did not pass" in response.json()["detail"]
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert durable["GSV_TTS_GPT_MODEL"] == str(old_gpt)
    assert factory_calls == []
    assert calls and calls[0]["path"] == "/v1/configure"
    assert calls[0]["json"]["preload"] is False
    from character_memory import settings_server, gsv_tts_runtime
    evidence = {
        "case": rejection, "http_status": response.status_code, "error": response.json(),
        "durable_gpt": durable["GSV_TTS_GPT_MODEL"], "sidecar_gpt": engine.gpt_model,
        "sidecar_loaded_before": True, "sidecar_loaded_after": engine.status()["loaded"],
        "files_unchanged": True, "configure_calls": calls, "model_factory_calls": len(factory_calls),
        "source_sha256": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for module in (settings_server, gsv_tts_runtime)},
        "source_files": [str(Path(module.__file__).resolve()) for module in (settings_server, gsv_tts_runtime)],
        "boundary": "Real routes/configure; dummy assets + sentinel, no GPU or model inference",
    }
    evidence_dir = Path(os.getenv("SETTINGS_AUDIT_EVIDENCE_DIR",
                                 "artifacts/core-visual-evidence/settings-gsv-rejection"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    name = f"gsv-rejection-{rejection}{'-restore-failure' if fail_restore else ''}.json"
    (evidence_dir / name).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if not fail_restore and engine.gpt_model != str(old_gpt):
        raise RejectedSaveLeftRuntimeChanged(
            f"PATCH 400 left disk=A but sidecar=B ({rejection}); configure calls={len(calls)}")
