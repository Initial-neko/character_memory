from __future__ import annotations

import copy
import os
from pathlib import Path
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from character_memory.settings_store import GSV_RUNTIME_FIELDS, SettingsStore


FORMAL_TTS_PROVIDER_IDS = ("kokoro", "sherpa", "edge", "gsv")


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SecretUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=20000)


class TtsPreviewRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    voice: str = Field(default="", max_length=128)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    text: str = Field(default="你好，这是当前语音配置的试听。", min_length=1, max_length=500)


def create_settings_app(config_path: str = "config.yaml", *, store: SettingsStore | None = None, runtime_http_client=None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, Response
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("Settings Center requires the api extra") from exc

    settings_store = store or SettingsStore(config_path)
    migration = settings_store.migrate_legacy_secrets()
    web_dir = Path(__file__).with_name("web")
    app = FastAPI(title="Character Memory Settings Center", version="0.1")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    runtime_urls = {
        "character": os.getenv("CHARACTER_SETTINGS_CHARACTER_BASE", "http://127.0.0.1:8000").rstrip("/"),
        "media": os.getenv("CHARACTER_SETTINGS_MEDIA_BASE", "http://127.0.0.1:8001").rstrip("/"),
        "dev": os.getenv("CHARACTER_SETTINGS_DEV_BASE", "http://127.0.0.1:8002").rstrip("/"),
        "tts_lab": os.getenv("CHARACTER_SETTINGS_TTS_LAB_BASE", "http://127.0.0.1:9002").rstrip("/"),
        "gsv": os.getenv("CHARACTER_SETTINGS_GSV_BASE", "http://127.0.0.1:9014").rstrip("/"),
    }
    owns_client = runtime_http_client is None
    client = runtime_http_client or httpx.Client(timeout=3.0)

    def tts_inventory() -> dict[str, Any]:
        providers: list[dict[str, Any]] = []
        errors: list[str] = []
        for provider_id in FORMAL_TTS_PROVIDER_IDS:
            try:
                response = client.get(
                    runtime_urls["tts_lab"] + f"/v1/providers/{provider_id}",
                    timeout=1.0,
                )
                response.raise_for_status()
                raw = dict((response.json() or {}).get("provider") or {})
                voices = [str(value) for value in (raw.get("voices") or []) if str(value).strip()]
                default_voice = str(raw.get("default_voice") or (voices[0] if voices else "")).strip()
                providers.append(
                    {
                        "id": provider_id,
                        "label": str(raw.get("label") or provider_id),
                        "ready": bool(raw.get("ready")),
                        "loaded": bool(raw.get("loaded")),
                        "voices": voices,
                        "default_voice": default_voice,
                        "supports_speed": raw.get("supports_speed") is not False,
                        "model": raw.get("model"),
                        "device": raw.get("device"),
                        "reason": raw.get("reason"),
                        "note": raw.get("note"),
                    }
                )
            except Exception as exc:
                message = f"{provider_id}: {exc}"
                errors.append(message)
                providers.append(
                    {
                        "id": provider_id,
                        "label": provider_id,
                        "ready": False,
                        "loaded": False,
                        "voices": [],
                        "default_voice": "",
                        "supports_speed": False,
                        "model": None,
                        "device": None,
                        "reason": f"health check failed: {exc}",
                        "note": None,
                    }
                )
        return {"ok": not errors, "providers": providers, "error": "; ".join(errors) if errors else None}

    def _gsv_payload(values: dict[str, Any] | None = None, *, preload: bool | None = None) -> dict[str, Any]:
        snapshot = settings_store.snapshot()["values"]
        merged = dict(snapshot)
        if values:
            merged.update(values)
        payload = {
            "gpt_model": str(merged.get("GSV_TTS_GPT_MODEL") or "").strip(),
            "sovits_model": str(merged.get("GSV_TTS_SOVITS_MODEL") or "").strip(),
            "ref_audio": str(merged.get("GSV_TTS_REF_AUDIO") or "").strip(),
            "ref_text": str(merged.get("GSV_TTS_REF_TEXT") or "").strip(),
            "voice": str(merged.get("GSV_TTS_VOICE") or "murasame").strip() or "murasame",
            "device": (str(merged.get("tts_device") or "cuda").strip() or "cuda") if str(merged.get("tts_provider") or "").strip().lower() == "gsv" else "cuda",
        }
        if preload is not None:
            payload["preload"] = bool(preload)
        return payload

    def _configure_gsv(values: dict[str, Any], *, preload: bool) -> dict[str, Any]:
        response = client.post(
            runtime_urls["gsv"] + "/v1/configure",
            json=_gsv_payload(values, preload=preload),
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json() or {}

    def _load_gsv() -> None:
        response = client.post(runtime_urls["gsv"] + "/v1/load", timeout=180.0)
        response.raise_for_status()

    def _unload_gsv() -> None:
        try:
            response = client.post(runtime_urls["gsv"] + "/v1/unload", timeout=10.0)
            response.raise_for_status()
        except Exception:
            pass

    def settings_snapshot() -> dict[str, Any]:
        snapshot = settings_store.snapshot()
        inventory = tts_inventory()
        providers = list(inventory["providers"])
        current_provider = str(snapshot["values"].get("tts_provider") or "").strip().lower()
        current_voice = str(snapshot["values"].get("tts_voice") or "").strip()
        selected = next((item for item in providers if item["id"] == current_provider), None)
        schema = copy.deepcopy(snapshot["schema"])
        voice_section = next((section for section in schema if section.get("id") == "voice"), None)
        if voice_section is not None:
            provider_field = next((field for field in voice_section.get("fields", []) if field.get("name") == "tts_provider"), None)
            voice_field = next((field for field in voice_section.get("fields", []) if field.get("name") == "tts_voice"), None)
            provider_options = [
                {
                    "value": item["id"],
                    "label": item["label"] if item["ready"] else f"{item['label']} · unavailable",
                    "disabled": not item["ready"],
                    "ready": item["ready"],
                }
                for item in providers
            ]
            if current_provider and not any(option["value"] == current_provider for option in provider_options):
                provider_options.insert(
                    0,
                    {
                        "value": current_provider,
                        "label": f"{current_provider} (当前配置 · 未通过健康检查)",
                        "disabled": True,
                        "ready": False,
                    },
                )
            if provider_field is not None:
                provider_field["options"] = provider_options

            voices = list((selected or {}).get("voices") or [])
            voice_options = [
                {"value": value, "label": value, "disabled": not bool(selected and selected.get("ready"))}
                for value in voices
            ]
            if current_voice and current_voice not in voices:
                voice_options.insert(
                    0,
                    {
                        "value": current_voice,
                        "label": f"{current_voice} (当前配置)",
                        "disabled": not bool(selected and selected.get("ready")),
                    },
                )
            if voice_field is not None:
                voice_field["options"] = voice_options

        snapshot["schema"] = schema
        snapshot["tts"] = {
            **inventory,
            "selected_provider": current_provider,
            "selected_voice": current_voice,
        }
        return snapshot

    def _healthy_provider(provider_id: str) -> dict[str, Any]:
        inventory = tts_inventory()
        provider = next((item for item in inventory["providers"] if item["id"] == provider_id), None)
        if provider is None or not provider.get("ready"):
            reason = (provider or {}).get("reason") or inventory.get("error") or "provider health check failed"
            raise ValueError(f"TTS provider {provider_id!r} is not selectable because its health check did not pass: {reason}")
        return provider

    @staticmethod
    def _runtime_device_value(provider: dict[str, Any]) -> str | None:
        value = str(provider.get("device") or "").strip().lower()
        if value.startswith("cuda"):
            return "cuda"
        if value.startswith("cpu"):
            return "cpu"
        return None

    def validated_tts_values(values: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        if not ({"tts_provider", "tts_voice", "tts_device"} & set(normalized)):
            return normalized

        current = settings_store.snapshot()["values"]
        current_provider = str(current.get("tts_provider") or "").strip().lower()
        provider_id = str(normalized.get("tts_provider", current_provider) or "").strip().lower()
        provider_changed = "tts_provider" in normalized and provider_id != current_provider
        provider = _healthy_provider(provider_id)

        voices = list(provider.get("voices") or [])
        default_voice = str(provider.get("default_voice") or (voices[0] if voices else "")).strip()
        voice = str(normalized.get("tts_voice", current.get("tts_voice", "")) or "").strip()
        if provider_changed and (not voice or (voices and voice not in voices)):
            voice = default_voice
            normalized["tts_voice"] = voice
        elif voice and voices and voice not in voices:
            raise ValueError(
                f"TTS voice {voice!r} is not available for healthy provider {provider_id!r}. "
                f"Available: {', '.join(voices)}"
            )
        elif provider_changed and "tts_voice" not in normalized:
            normalized["tts_voice"] = voice or default_voice

        runtime_device = _runtime_device_value(provider)
        if provider_changed and runtime_device:
            normalized["tts_device"] = runtime_device

        return normalized

    @app.on_event("shutdown")
    def shutdown():
        if owns_client:
            client.close()

    @app.get("/")
    @app.get("/settings")
    def index():
        return FileResponse(web_dir / "settings.html")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "service": "character-settings",
            "config_path": str(settings_store.config_path),
            "env_path": str(settings_store.env_path),
            "legacy_secret_migration": migration,
        }

    @app.get("/v1/settings")
    def get_settings():
        try:
            return settings_snapshot()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取配置失败：{exc}") from exc

    @app.patch("/v1/settings")
    def patch_settings(req: SettingsPatch):
        try:
            raw_values = dict(req.values)
            before = settings_store.snapshot()["values"]
            before_provider = str(before.get("tts_provider") or "").strip().lower()
            requested_provider = str(raw_values.get("tts_provider", before_provider) or "").strip().lower()
            if requested_provider == "gsv" and before_provider != "gsv":
                # Do not inherit a stale CPU setting from Kokoro/Sherpa when
                # switching into the validated realtime GSV CUDA path.
                raw_values["tts_device"] = "cuda"
            gsv_updates = set(raw_values) & GSV_RUNTIME_FIELDS

            # Configure the already-running GSV sidecar before provider health
            # validation. This lets one Save action fill the missing GSV assets
            # and select GSV without restarting the whole stack.
            if gsv_updates:
                _configure_gsv(raw_values, preload=requested_provider == "gsv")

            values = validated_tts_values(raw_values)
            result = settings_store.save_values(values)

            after = settings_store.snapshot()["values"]
            after_provider = str(after.get("tts_provider") or "").strip().lower()
            if after_provider == "gsv" and not gsv_updates:
                _load_gsv()
            elif before_provider == "gsv" and after_provider != "gsv":
                _unload_gsv()

            return {"ok": True, "result": result, "settings": settings_snapshot()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text or str(exc)
            raise HTTPException(status_code=400, detail=f"GSV runtime configuration failed: {detail}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"保存配置失败：{exc}") from exc

    @app.post("/v1/tts-preview")
    def tts_preview(req: TtsPreviewRequest):
        provider_id = str(req.provider or "").strip().lower()
        try:
            provider = _healthy_provider(provider_id)
            voices = list(provider.get("voices") or [])
            voice = str(req.voice or provider.get("default_voice") or "").strip()
            if voices and voice not in voices:
                voice = str(provider.get("default_voice") or voices[0]).strip()
            response = client.post(
                runtime_urls["tts_lab"] + "/v1/tts",
                json={
                    "provider": provider_id,
                    "text": req.text,
                    "voice": voice,
                    "speed": float(req.speed),
                },
                timeout=180.0,
            )
            response.raise_for_status()
            media_type = (response.headers.get("content-type") or "audio/wav").split(";", 1)[0]
            return Response(
                content=response.content,
                media_type=media_type,
                headers={
                    "X-TTS-Provider": provider_id,
                    "X-TTS-Voice": response.headers.get("x-tts-voice", voice),
                    "X-TTS-Device": response.headers.get("x-tts-device", str(provider.get("device") or "")),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"TTS preview failed: {exc}") from exc

    @app.put("/v1/settings/secrets/{name}")
    def put_secret(name: str, req: SecretUpdate):
        try:
            result = settings_store.save_secret(name, req.value)
            return {"ok": True, "result": result, "secrets": settings_store.secret_statuses()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"保存 Secret 失败：{exc}") from exc

    @app.delete("/v1/settings/secrets/{name}")
    def delete_secret(name: str):
        try:
            result = settings_store.delete_secret(name)
            return {"ok": True, "result": result, "secrets": settings_store.secret_statuses()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"删除 Secret 失败：{exc}") from exc

    @app.post("/v1/settings/migrate")
    def migrate():
        try:
            result = settings_store.migrate_legacy_secrets()
            return {"ok": True, "result": result, "settings": settings_snapshot()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"配置迁移失败：{exc}") from exc

    def probe(base_url: str, path: str = "/health") -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = client.get(base_url + path)
            total_ms = round((time.perf_counter() - started) * 1000.0, 1)
            try:
                payload = response.json()
            except ValueError:
                payload = {"text": (response.text or "")[:400]}
            return {
                "ok": response.is_success,
                "status_code": response.status_code,
                "total_ms": total_ms,
                "data": payload,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": None,
                "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "error": str(exc),
            }

    @app.get("/v1/runtime-status")
    def runtime_status():
        return {
            "character": probe(runtime_urls["character"]),
            "media": probe(runtime_urls["media"]),
            "dev": probe(runtime_urls["dev"]),
            # /tts is deliberately shallow; /health probes optional providers.
            "tts_lab": probe(runtime_urls["tts_lab"], "/tts"),
        }

    return app


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Settings Center requires the api extra") from exc
    config_path = os.getenv("CHARACTER_CONFIG_PATH", os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml"))
    host = os.getenv("CHARACTER_SETTINGS_HOST", "127.0.0.1")
    port = int(os.getenv("CHARACTER_SETTINGS_PORT", "8003"))
    print(f"settings: http://{host}:{port}/settings")
    uvicorn.run(
        create_settings_app(config_path),
        host=host,
        port=port,
        reload=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
