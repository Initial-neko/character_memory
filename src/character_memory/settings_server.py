from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from character_memory.settings_store import SettingsStore


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SecretUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=20000)


def create_settings_app(config_path: str = "config.yaml", *, store: SettingsStore | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse
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
    }
    client = httpx.Client(timeout=3.0)

    @app.on_event("shutdown")
    def shutdown():
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
            return settings_store.snapshot()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取配置失败：{exc}") from exc

    @app.patch("/v1/settings")
    def patch_settings(req: SettingsPatch):
        try:
            result = settings_store.save_values(req.values)
            return {"ok": True, "result": result, "settings": settings_store.snapshot()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"保存配置失败：{exc}") from exc

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
            return {"ok": True, "result": result, "settings": settings_store.snapshot()}
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
