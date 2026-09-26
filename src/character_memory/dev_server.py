from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.app import build_model
from character_memory.config import Settings, load_settings
from character_memory.llm.usage import LlmUsageStore
from character_memory.resource_metrics import collect_resource_snapshot
from character_memory.web_assets import attach_static_assets


logger = logging.getLogger("character_memory.dev_server")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _upstream_detail(response: httpx.Response, operation: str, *, service: str = "media-runtime") -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
    elif payload is not None:
        detail = payload
    else:
        detail = (response.text or f"upstream {operation} failed")[:4000]
    return {
        "service": service,
        "operation": operation,
        "status_code": response.status_code,
        "detail": detail,
    }


class DevLlmRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    system_prompt: str = Field(default="", max_length=4000)


class DevVisionRequest(DevLlmRequest):
    image_data_urls: list[str] = Field(min_length=1, max_length=5)


class DevMediaSmokeRequest(BaseModel):
    text: str = Field(default="你好，这是 Character Memory 的媒体自检。", min_length=1, max_length=4000)


class DevImageGenRequest(BaseModel):
    character_id: str = Field(default="rin", min_length=1, max_length=64)
    provider: str = Field(default="", max_length=32)
    purpose: str = Field(default="SELFIE", max_length=16)
    visual_intent: str = Field(default="自然分享一下现在的样子", min_length=1, max_length=1600)
    use_avatar_reference: bool = True


class DevAvatarFromMediaRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    media_id: str = Field(min_length=1, max_length=80)


class DevSpaceConfigRequest(BaseModel):
    enabled: bool
    interval_minutes: float = Field(ge=10.0, le=10080.0)
    max_posts_per_day: int = Field(ge=0, le=200)
    media_enabled: bool = True
    media_max_items: int = Field(default=3, ge=0, le=9)
    image_search_enabled: bool = True
    image_generation_enabled: bool = True
    world_observation_enabled: bool = True
    world_max_pages: int = Field(default=2, ge=1, le=4)
    world_max_chars_per_page: int = Field(default=6000, ge=500, le=16000)
    audience_size: int = Field(ge=0, le=10)
    poll_seconds: float = Field(ge=10.0, le=3600.0)
    rearm: bool = True


class DevGroupAutonomyConfigRequest(BaseModel):
    enabled: bool
    interval_minutes: float = Field(ge=10.0, le=10080.0)
    max_messages: int = Field(ge=1, le=4)
    user_quiet_minutes: float = Field(ge=0.0, le=1440.0)
    poll_seconds: float = Field(ge=10.0, le=3600.0)
    rearm: bool = True


class DevSpaceMediaRequest(BaseModel):
    type: str = Field(pattern=r"^(SEARCH_IMAGE|GENERATE_IMAGE|VOICE)$")
    content: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=9)
    query: str = Field(default="", max_length=300)
    purpose: str = Field(default="SCENE", pattern=r"^(SELFIE|SCENE)$")
    visual_intent: str = Field(default="", max_length=800)
    voice_text: str = Field(default="", max_length=4000)


class DevWorldFetchRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=6000, ge=500, le=16000)


class DevWorldSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=240)
    max_pages: int = Field(default=2, ge=1, le=4)
    max_chars_per_page: int = Field(default=6000, ge=500, le=16000)


class DevAsrCaptureModeRequest(BaseModel):
    """Test-mode switch, forwarded to the Media Runtime that owns the captures."""

    enabled: bool | None = None
    clear: bool = False


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

    def request_character(method: str, path: str, *, operation: str, json: dict | None = None, timeout: float = 120.0):
        try:
            response = client.request(method, f"{character_base}{path}", json=json, timeout=timeout)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"service": "character-runtime", "operation": operation, "detail": f"request failed: {exc}"},
            ) from exc
        if response.is_error:
            raise HTTPException(
                status_code=response.status_code,
                detail=_upstream_detail(response, operation, service="character-runtime"),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"Character Runtime returned non-JSON response for {operation}") from exc

    def request_tts(payload: dict):
        try:
            response = client.post(f"{media_base}/v1/tts", json=payload, timeout=120.0)
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

    app = FastAPI(title="Character Memory Dev Console", version="0.4")
    attach_static_assets(app, web_dir)

    @on_app_event(app, "shutdown")
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
            "visual": probe(f"{character_base}/v1/visual/providers"),
        }

    @app.get("/v1/dev/resources")
    def resources():
        character_probe = probe(f"{character_base}/health")
        media_probe = probe(f"{media_base}/health")
        character_health = character_probe.get("data") if character_probe.get("ok") else None
        media_health = media_probe.get("data") if media_probe.get("ok") else None
        snapshot = collect_resource_snapshot(
            character_base_url=character_base,
            media_base_url=media_base,
            character_health=character_health,
            media_health=media_health,
        )
        snapshot["sampled_at"] = time.time()
        return snapshot

    @app.get("/v1/dev/characters")
    def dev_characters():
        return request_character("GET", "/v1/characters", operation="characters", timeout=10.0)

    @app.get("/v1/dev/groups")
    def dev_groups():
        return request_character("GET", "/v1/groups", operation="groups", timeout=10.0)

    @app.get("/v1/dev/group-autonomy/status")
    def dev_group_autonomy_status():
        return request_character(
            "GET",
            "/v1/group-autonomy/status",
            operation="group-autonomy-status",
            timeout=10.0,
        )

    @app.post("/v1/dev/group-autonomy/config")
    def dev_group_autonomy_config(req: DevGroupAutonomyConfigRequest):
        runtime = request_character(
            "POST",
            "/v1/group-autonomy/config",
            operation="group-autonomy-config",
            json=req.model_dump(),
            timeout=10.0,
        )
        return {
            "scope": "runtime-only",
            "persisted": False,
            "note": "Dev Console overrides only the current Character Runtime. Persist formal values in Settings Center.",
            "runtime": runtime,
        }

    @app.post("/v1/dev/group-autonomy/due/{conversation_id}")
    def dev_group_autonomy_due(conversation_id: str):
        safe_id = quote(conversation_id, safe="")
        return request_character(
            "POST",
            f"/v1/group-autonomy/due/{safe_id}",
            operation="group-autonomy-due",
            timeout=10.0,
        )

    @app.post("/v1/dev/group-autonomy/opportunity/{conversation_id}")
    def dev_group_autonomy_opportunity(conversation_id: str):
        safe_id = quote(conversation_id, safe="")
        return request_character(
            "POST",
            f"/v1/group-autonomy/opportunity/{safe_id}",
            operation="group-autonomy-opportunity",
            timeout=300.0,
        )

    @app.get("/v1/dev/space/status")
    def dev_space_status():
        return request_character(
            "GET",
            "/v1/space/dev/status",
            operation="space-status",
            timeout=10.0,
        )

    @app.get("/v1/dev/space/opportunity/run/{run_id}")
    def dev_space_opportunity_run(run_id: int):
        return request_character(
            "GET",
            f"/v1/space/dev/opportunity/run/{int(run_id)}",
            operation="space-run-detail",
            timeout=10.0,
        )


    @app.post("/v1/dev/space/config")
    def dev_space_config(req: DevSpaceConfigRequest):
        runtime = request_character(
            "POST",
            "/v1/space/dev/config",
            operation="space-config",
            json=req.model_dump(),
            timeout=10.0,
        )
        return {
            "scope": "runtime-only",
            "persisted": False,
            "note": "Dev Console overrides only the current Character Runtime. Persist formal values in Settings Center.",
            "runtime": runtime,
        }

    @app.get("/v1/dev/encounters/status")
    def dev_encounter_status():
        return request_character(
            "GET",
            "/v1/encounters/status",
            operation="encounter-status",
            timeout=10.0,
        )

    @app.post("/v1/dev/encounters/due")
    def dev_encounter_due():
        return request_character(
            "POST",
            "/v1/encounters/dev/due",
            operation="encounter-due",
            timeout=10.0,
        )

    @app.post("/v1/dev/encounters/opportunity")
    def dev_encounter_opportunity(source_type: str = "AUTO"):
        normalized = str(source_type or "AUTO").strip().upper()
        if normalized not in {"AUTO", "WEB", "GENERATED"}:
            raise HTTPException(status_code=400, detail="source_type must be AUTO, WEB or GENERATED")
        return request_character(
            "POST",
            "/v1/encounters/dev/opportunity",
            operation="encounter-opportunity",
            json={"source_type": normalized},
            timeout=300.0,
        )

    @app.get("/v1/dev/world/status")
    def dev_world_status():
        return request_character("GET", "/v1/world/status", operation="world-status", timeout=10.0)

    @app.post("/v1/dev/world/fetch")
    def dev_world_fetch(req: DevWorldFetchRequest):
        return request_character(
            "POST",
            "/v1/world/dev/fetch",
            operation="world-fetch",
            json=req.model_dump(),
            timeout=max(30.0, float(getattr(cfg, "web_browser_timeout_seconds", 20.0)) + 15.0),
        )

    @app.post("/v1/dev/world/search")
    def dev_world_search(req: DevWorldSearchRequest):
        timeout = (
            max(30.0, float(getattr(cfg, "web_browser_timeout_seconds", 20.0)) + 10.0)
            * max(1, int(req.max_pages))
        )
        return request_character(
            "POST",
            "/v1/world/dev/search",
            operation="world-search",
            json=req.model_dump(),
            timeout=timeout,
        )

    @app.get("/v1/dev/world/activity")
    def dev_world_activity_status():
        return request_character(
            "GET",
            "/v1/world/activity/status",
            operation="world-activity-status",
            timeout=10.0,
        )

    @app.get("/v1/dev/world/pulse")
    def dev_world_pulse():
        return request_character(
            "GET",
            "/v1/world/pulse?limit=20",
            operation="world-pulse",
            timeout=10.0,
        )

    @app.post("/v1/dev/world/pulse/refresh")
    def dev_world_pulse_refresh():
        timeout = max(
            60.0,
            float(getattr(cfg, "web_browser_timeout_seconds", 20.0))
            * max(1, len(getattr(cfg, "world_pulse_sources", []) or [])),
        )
        return request_character(
            "POST",
            "/v1/world/pulse/dev/refresh",
            operation="world-pulse-refresh",
            timeout=timeout,
        )

    @app.post("/v1/dev/world/pulse/{topic_id}/discuss")
    def dev_world_pulse_discuss(topic_id: int):
        return request_character(
            "POST",
            f"/v1/world/pulse/{topic_id}/dev/discuss",
            operation="world-pulse-discuss",
            timeout=300.0,
        )

    @app.post("/v1/dev/world/browse/{character_id}")
    def dev_world_personal_browse(character_id: str):
        safe_id = quote(character_id, safe="")
        return request_character(
            "POST",
            f"/v1/world/dev/browse/{safe_id}",
            operation="world-personal-browse",
            timeout=300.0,
        )

    @app.post("/v1/dev/world/activity/run")
    def dev_world_activity_run():
        return request_character(
            "POST",
            "/v1/world/activity/dev/run",
            operation="world-activity-run",
            timeout=300.0,
        )

    @app.post("/v1/dev/space/due/{character_id}")
    def dev_space_force_due(character_id: str):
        safe_id = quote(character_id, safe="")
        return request_character(
            "POST",
            f"/v1/space/dev/due/{safe_id}",
            operation="space-force-due",
            timeout=10.0,
        )

    @app.post("/v1/dev/space/opportunity/{character_id}")
    def dev_space_opportunity(character_id: str):
        safe_id = quote(character_id, safe="")
        return request_character(
            "POST",
            f"/v1/space/dev/opportunity/{safe_id}",
            operation="space-opportunity",
            timeout=300.0,
        )

    @app.post("/v1/dev/space/media/{character_id}")
    def dev_space_media(character_id: str, req: DevSpaceMediaRequest):
        safe_id = quote(character_id, safe="")
        return request_character(
            "POST",
            f"/v1/space/dev/media/{safe_id}",
            operation="space-media",
            json=req.model_dump(),
            timeout=max(120.0, float(getattr(cfg, "image_generation_timeout_seconds", 180.0)) + 60.0),
        )

    @app.post("/v1/dev/space/audience/{post_id}")
    def dev_space_audience(post_id: int):
        return request_character(
            "POST",
            f"/v1/space/dev/audience/{int(post_id)}",
            operation="space-audience",
            timeout=300.0,
        )

    @app.get("/v1/dev/visual/providers")
    def dev_visual_providers():
        return request_character("GET", "/v1/visual/providers", operation="visual-providers", timeout=10.0)

    def visual_request_payload(req: DevImageGenRequest) -> dict:
        return {
            "instruction": req.visual_intent,
            "provider": req.provider,
            "purpose": req.purpose,
            "use_avatar_reference": req.use_avatar_reference,
        }

    @app.post("/v1/dev/imagegen/rewrite")
    def dev_imagegen_rewrite(req: DevImageGenRequest):
        character_id = quote(req.character_id, safe="")
        return request_character(
            "POST",
            f"/v1/characters/{character_id}/images/rewrite",
            operation="imagegen-rewrite",
            json=visual_request_payload(req),
            timeout=120.0,
        )

    @app.post("/v1/dev/imagegen")
    def dev_imagegen(req: DevImageGenRequest):
        timeout = max(120.0, float(getattr(cfg, "image_generation_timeout_seconds", 180.0)) + 30.0)
        character_id = quote(req.character_id, safe="")
        payload = visual_request_payload(req)
        payload["persist_result"] = True
        data = request_character(
            "POST",
            f"/v1/characters/{character_id}/images/generate",
            operation="imagegen",
            json=payload,
            timeout=timeout,
        )
        image = data.get("image") if isinstance(data, dict) else None
        if isinstance(image, dict):
            image.pop("data_url", None)
            url = str(image.get("url") or "")
            if url.startswith("/"):
                image["url"] = f"{character_base}{url}"
        return data

    @app.post("/v1/dev/avatar-from-media")
    def dev_avatar_from_media(req: DevAvatarFromMediaRequest):
        character_id = quote(req.character_id, safe="")
        return request_character(
            "POST",
            f"/v1/characters/{character_id}/avatar/from-chat",
            operation="avatar-from-media",
            json={"media_id": req.media_id},
            timeout=30.0,
        )

    @app.post("/v1/dev/llm")
    def llm(req: DevLlmRequest):
        started = time.perf_counter()
        try:
            model = get_model()
            messages: list[dict] = []
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

    @app.post("/v1/dev/vision")
    def vision(req: DevVisionRequest):
        started = time.perf_counter()
        try:
            if any(not value.startswith("data:image/") for value in req.image_data_urls):
                raise ValueError("Vision test accepts image data URLs only")
            model = get_model()
            selected_model = str(getattr(model, "vision_model", "") or getattr(cfg, "vision_model", "") or "")
            if not selected_model:
                raise RuntimeError("vision_model is not configured")
            request = getattr(model, "_request", None)
            if not callable(request):
                raise RuntimeError("configured model does not expose the OpenAI-compatible request path")
            messages: list[dict] = []
            if req.system_prompt.strip():
                messages.append({"role": "system", "content": req.system_prompt.strip()})
            content: list[dict] = [{"type": "text", "text": req.prompt}]
            content.extend({"type": "image_url", "image_url": {"url": value}} for value in req.image_data_urls)
            messages.append({"role": "user", "content": content})
            reply = request(
                messages,
                conversation_id="dev-console-vision",
                model=selected_model,
            )
            return {
                "ok": True,
                "kind": "vision",
                "model": selected_model,
                "frame_count": len(req.image_data_urls),
                "reply": reply,
                "total_ms": _ms(started),
            }
        except Exception as exc:
            logger.exception("dev.vision failed error=%s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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
        tts_response = request_tts({"text": req.text})
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

    @app.get("/v1/dev/llm-usage")
    def llm_usage(
        hours: int = Query(default=24, ge=1, le=2160),
        limit: int = Query(default=80, ge=1, le=300),
    ):
        store = LlmUsageStore(cfg.db_path)
        try:
            return store.usage(hours=hours, limit=limit)
        finally:
            store.close()

    @app.get("/v1/dev/metrics")
    def metrics(limit: int = Query(default=50, ge=1, le=200)):
        try:
            response = client.get(f"{media_base}/v1/metrics/recent", params={"limit": limit}, timeout=10.0)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Media Runtime request failed: {exc}") from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "metrics"))
        return response.json()

    # The captures live in the Media Runtime because that is where a WAV arrives.
    # These three routes only carry them across so the panel has one origin.
    @app.get("/v1/dev/asr-capture")
    def asr_capture(limit: int = Query(default=100, ge=1, le=500)):
        try:
            response = client.get(f"{media_base}/v1/dev/asr-capture", params={"limit": limit}, timeout=10.0)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Media Runtime request failed: {exc}") from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "asr-capture"))
        return response.json()

    @app.get("/v1/dev/asr-capture/{item_id}/audio")
    def asr_capture_audio(item_id: str):
        try:
            response = client.get(f"{media_base}/v1/dev/asr-capture/{quote(item_id, safe='')}/audio", timeout=30.0)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Media Runtime request failed: {exc}") from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "asr-capture-audio"))
        return Response(content=response.content, media_type="audio/wav")

    @app.post("/v1/dev/asr-capture/test-mode")
    def asr_capture_test_mode(req: DevAsrCaptureModeRequest):
        try:
            response = client.post(f"{media_base}/v1/dev/asr-capture/test-mode", json=req.model_dump(), timeout=10.0)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Media Runtime request failed: {exc}") from exc
        if response.is_error:
            raise HTTPException(status_code=response.status_code, detail=_upstream_detail(response, "asr-capture-test-mode"))
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
