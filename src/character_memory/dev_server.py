from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

import httpx
from pydantic import BaseModel, Field

from character_memory.app import build_model
from character_memory.config import Settings, load_settings


logger = logging.getLogger("character_memory.dev_server")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _upstream_detail(response: httpx.Response, operation: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
    elif payload is not None:
        detail = payload
    else:
        detail = (response.text or f"Media Runtime {operation} failed")[:4000]
    return {
        "service": "media-runtime",
        "operation": operation,
        "status_code": response.status_code,
        "detail": detail,
    }


class DevLlmRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    system_prompt: str = Field(default="", max_length=4000)


class DevTtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    speaker_id: int = Field(default=0, ge=0, le=10000)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class DevMediaSmokeRequest(DevTtsRequest):
    text: str = Field(default="你好，这是 Character Memory 的媒体自检。", min_length=1, max_length=4000)


def create_dev_app(
    config_path: str = "config.yaml",
    *,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    model_factory: Callable[[Settings], object] | None = None,
):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Query
        from fastapi.responses import FileResponse, Response
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("Dev Console requires the api extra") from exc

    cfg = settings or load_settings(config_path)
    client = http_client or httpx.Client(timeout=120.0)
    owns_client = http_client is None
    make_model = model_factory or build_model
    model_holder: dict[str, object] = {}
    model_lock = threading.Lock()
    web_dir = Path(__file__).with_name("web")

    character_base = os.getenv("CHARACTER_DEV_CHARACTER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    media_base = os.getenv("CHARACTER_DEV_MEDIA_BASE_URL", "http://127.0.0.1:8001").rstrip("/")

    def get_model():
        model = model_holder.get("model")
        if model is not None:
            return model
        with model_lock:
            model = model_holder.get("model")
            if model is None:
                model = make_model(cfg)
                model_holder["model"] = model
            return model

    def probe(url: str) -> dict:
        started = time.perf_counter()
        try:
            response = client.get(url, timeout=3.0)
            try:
                payload = response.json()
            except ValueError:
                payload = {"text": (response.text or "")[:1000]}
            return {
                "ok": response.is_success and bool(payload.get("ok", True)),
                "status_code": response.status_code,
                "total_ms": _ms(started),
                "data": payload,
            }
        except Exception as exc:
            return {"ok": False, "status_code": None, "total_ms": _ms(started), "error": str(exc)}

    def request_tts(req: DevTtsRequest):
        try:
            response = client.post(f"{media_base}/v1/tts", json=req.model_dump(), timeout=120.0)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"service": "media-runtime", "operation": "tts", "detail": f"request failed: {exc}"},
            ) from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "tts"))
        return response

    def request_asr(payload: bytes, content_type: str = "audio/wav"):
        try:
            response = client.post(
                f"{media_base}/v1/asr",
                content=payload,
                headers={"Content-Type": content_type},
                timeout=120.0,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"service": "media-runtime", "operation": "asr", "detail": f"request failed: {exc}"},
            ) from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "asr"))
        return response

    app = FastAPI(title="Character Memory Dev Console", version="0.2")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("shutdown")
    def shutdown():
        model = model_holder.get("model")
        if model is not None:
            close = getattr(model, "close", None)
            if callable(close):
                close()
        if owns_client:
            client.close()

    @app.get("/")
    @app.get("/dev")
    def index():
        return FileResponse(web_dir / "dev.html")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "service": "character-dev",
            "character_base_url": character_base,
            "media_base_url": media_base,
            "model": cfg.chat_model,
            "vision_model": getattr(cfg, "vision_model", None) or cfg.chat_model,
        }

    @app.get("/v1/dev/status")
    def status():
        return {
            "ok": True,
            "dev": {
                "model": cfg.chat_model,
                "vision_model": getattr(cfg, "vision_model", None) or cfg.chat_model,
                "base_url": cfg.base_url,
                "embedding_provider": cfg.embedding_provider,
                "llm_configured": bool(cfg.api_key),
            },
            "character": probe(f"{character_base}/health"),
            "media": probe(f"{media_base}/health"),
        }

    @app.post("/v1/dev/llm")
    def llm(req: DevLlmRequest):
        started = time.perf_counter()
        try:
            model = get_model()
            messages: list[dict[str, str]] = []
            if req.system_prompt.strip():
                messages.append({"role": "system", "content": req.system_prompt.strip()})
            messages.append({"role": "user", "content": req.prompt})
            request = getattr(model, "_request", None)
            if not callable(request):
                raise RuntimeError("configured model does not expose the OpenAI-compatible request path")
            reply = request(messages, conversation_id="dev-console")
            return {
                "ok": True,
                "kind": "llm",
                "model": str(getattr(model, "model", cfg.chat_model)),
                "reply": reply,
                "total_ms": _ms(started),
            }
        except Exception as exc:
            logger.exception("dev.llm failed error=%s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/dev/tts")
    def tts(req: DevTtsRequest):
        started = time.perf_counter()
        response = request_tts(req)
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower().startswith("x-media-")
        }
        headers["X-Dev-Total-Ms"] = str(_ms(started))
        return Response(content=response.content, media_type="audio/wav", headers=headers)

    @app.post("/v1/dev/asr")
    async def asr(
        payload: bytes = Body(..., media_type="application/octet-stream"),
        content_type: str | None = Header(default=None, alias="Content-Type"),
    ):
        if not payload:
            raise HTTPException(status_code=400, detail="empty audio")
        normalized = (content_type or "audio/wav").split(";", 1)[0].strip().lower()
        if normalized not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Dev Console ASR accepts WAV/PCM16")
        started = time.perf_counter()
        response = request_asr(payload, normalized)
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Media Runtime returned non-JSON ASR response") from exc
        data["http_total_ms"] = _ms(started)
        return data

    @app.post("/v1/dev/media-smoke")
    def media_smoke(req: DevMediaSmokeRequest):
        total_started = time.perf_counter()
        tts_started = time.perf_counter()
        tts_response = request_tts(req)
        tts_total_ms = _ms(tts_started)

        asr_started = time.perf_counter()
        asr_response = request_asr(tts_response.content, "audio/wav")
        asr_total_ms = _ms(asr_started)
        try:
            asr_data = asr_response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Media Runtime returned non-JSON ASR response during smoke test") from exc

        tts_inference = tts_response.headers.get("x-media-inference-ms")
        tts_audio_ms = tts_response.headers.get("x-media-audio-ms")
        return {
            "ok": True,
            "kind": "media-smoke",
            "input_text": req.text,
            "transcript": asr_data.get("text", ""),
            "tts": {
                "provider": tts_response.headers.get("x-media-provider"),
                "device": tts_response.headers.get("x-media-device"),
                "inference_ms": float(tts_inference) if tts_inference else None,
                "audio_ms": float(tts_audio_ms) if tts_audio_ms else None,
                "http_total_ms": tts_total_ms,
            },
            "asr": {
                **asr_data,
                "http_total_ms": asr_total_ms,
            },
            "total_ms": _ms(total_started),
        }

    @app.get("/v1/dev/metrics")
    def metrics(limit: int = Query(default=50, ge=1, le=200)):
        try:
            response = client.get(f"{media_base}/v1/metrics/recent", params={"limit": limit}, timeout=10.0)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Media Runtime request failed: {exc}") from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "metrics"))
        return response.json()

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Dev Console requires the api extra") from exc

    config_path = os.getenv("CHARACTER_CONFIG_PATH", "config.yaml")
    host = os.getenv("CHARACTER_DEV_HOST", "127.0.0.1")
    port = int(os.getenv("CHARACTER_DEV_PORT", "8002"))
    print(f"dev: http://{host}:{port}/dev")
    uvicorn.run(create_dev_app(config_path), host=host, port=port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
