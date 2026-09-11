from __future__ import annotations

import logging
import time


logger = logging.getLogger("character_memory.startup_web")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def attach_startup_routes(app) -> None:
    """Attach the small WebUI warm-up surface without changing chat ownership."""
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before startup routes are attached")

    @app.post("/v1/runtime/warmup")
    def warmup_runtime():
        started = time.perf_counter()
        try:
            bundle = access.get_bundle()
            stickers = access.global_sticker_catalog()
            access.refresh_runtime_sticker_catalog(stickers)
        except Exception as exc:
            logger.exception("startup.runtime warmup_failed error=%s", exc)
            raise HTTPException(status_code=503, detail=f"Runtime 初始化失败：{exc}") from exc

        total_ms = _ms(started)
        logger.info(
            "startup.runtime ready characters=%d stickers=%d warmup_ms=%.1f init_total_ms=%.1f",
            len(getattr(bundle, "characters", []) or []),
            len(getattr(stickers, "stickers", []) or []),
            total_ms,
            float(getattr(bundle, "init_timings", {}).get("total_ms", 0.0)),
        )
        return {
            "ready": True,
            "runtime_loaded": True,
            "characters": len(getattr(bundle, "characters", []) or []),
            "stickers": len(getattr(stickers, "stickers", []) or []),
            "warmup_ms": total_ms,
            "init_timings": dict(getattr(bundle, "init_timings", {}) or {}),
        }

    @app.get("/v1/runtime/warmup")
    def warmup_status():
        bundle = getattr(access, "get_bundle", None)
        runtime_loaded = False
        try:
            store = access.store()
            runtime_loaded = store is not access.read_store
        except Exception:
            runtime_loaded = False
        return {"ready": runtime_loaded, "runtime_loaded": runtime_loaded, "warmup_available": callable(bundle)}
