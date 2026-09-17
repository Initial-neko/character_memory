from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

from character_memory.config import load_settings
from character_memory.media_runtime import MediaRuntime, build_media_runtime_from_env


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # Legacy Sherpa request fields stay accepted. In production configured routing,
    # formal defaults are authoritative unless `voice` is explicitly supplied.
    speaker_id: int | None = Field(default=None, ge=0, le=10000)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    voice: str | None = Field(default=None, max_length=128)


def create_media_app(runtime: MediaRuntime | None = None):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("Media Runtime requires the api extra: pip install -e '.[api]'") from exc

    # Explicit runtime injection is the provider-neutral test/embedding contract.
    # Production startup passes no runtime and therefore uses configured routing.
    configured_routing = runtime is None
    media = runtime or build_media_runtime_from_env()
    config_path = os.getenv("CHARACTER_CONFIG_PATH", os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml"))
    settings = load_settings(config_path)
    tts_lab_base = os.getenv("CHARACTER_TTS_LAB_BASE", "http://127.0.0.1:9002").rstrip("/")
    provider_client = httpx.Client(timeout=180.0)

    app = FastAPI(title="Character Memory Media Runtime", version="0.2")
    app.state.media_runtime = media

    origins = [
        value.strip()
        for value in os.getenv(
            "CHARACTER_MEDIA_CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000",
        ).split(",")
        if value.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.on_event("shutdown")
    def shutdown():
        provider_client.close()

    def selected_tts_status(runtime_status: dict) -> dict:
        local_tts = dict(runtime_status.get("tts") or {})
        if not configured_routing:
            return {
                **local_tts,
                "provider": "injected-runtime",
                "voice": None,
                "speed": None,
                "device": local_tts.get("device"),
                "restart_required_for_config_changes": False,
            }

        selected = str(settings.tts_provider or "sherpa").strip().lower()
        base = {
            "provider": selected,
            "voice": settings.tts_voice,
            "speed": settings.tts_speed,
            "device": settings.tts_device,
            "restart_required_for_config_changes": True,
        }
        if selected != "kokoro":
            return {**local_tts, **base}

        # Do not call :9002/health here: its Sherpa audition provider checks this
        # Media Runtime and would create a health-check cycle. The provider page is
        # a cheap process/readiness boundary; actual synthesis still owns model
        # validation and returns a precise provider error if assets are unavailable.
        try:
            response = provider_client.get(f"{tts_lab_base}/tts", timeout=2.0)
            response.raise_for_status()
            return {**base, "ready": True, "loaded": None, "reason": None}
        except Exception as exc:
            return {
                **base,
                "ready": False,
                "loaded": False,
                "reason": f"Kokoro Provider Runtime unavailable at {tts_lab_base}: {exc}",
            }

    @app.get("/health")
    def health():
        status = media.status()
        status["tts_selected"] = selected_tts_status(status)
        return {"ok": True, **status}

    @app.post("/v1/asr")
    def transcribe(
        payload: bytes = Body(..., media_type="application/octet-stream"),
        content_type: str | None = Header(default=None, alias="Content-Type"),
    ):
        # Keep provider inference in FastAPI's worker threadpool. An async handler
        # that calls the blocking local recognizer directly would stall the event
        # loop and prevent TTS requests from overlapping ASR during voice playback.
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Voice V0 accepts audio/wav PCM16")
        max_bytes = int(os.getenv("CHARACTER_MEDIA_ASR_MAX_BYTES", str(4 * 1024 * 1024)))
        if not payload:
            raise HTTPException(status_code=400, detail="empty audio")
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail=f"audio exceeds {max_bytes} bytes")
        try:
            result = media.transcribe_wav(payload)
        except (RuntimeError, ValueError) as exc:
            code = 503 if isinstance(exc, RuntimeError) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/v1/tts")
    def synthesize(req: TtsRequest):
        selected = str(settings.tts_provider or "sherpa").strip().lower()
        explicit_voice = str(req.voice or "").strip()

        if configured_routing and selected == "kokoro":
            # :9002 is both the audition UI and the local provider service in V1.
            # Media Runtime remains the stable browser-facing TTS endpoint, so the
            # chat page does not need provider-specific URLs or CORS rules.
            voice = explicit_voice or str(settings.tts_voice or "zf_001")
            # Browser Voice V0 still carries legacy speed=1.0. Treat it as formal
            # config unless a caller also supplies an explicit voice override.
            speed = float(req.speed if explicit_voice and req.speed is not None else settings.tts_speed)
            try:
                response = provider_client.post(
                    f"{tts_lab_base}/v1/tts",
                    json={
                        "provider": "kokoro",
                        "text": req.text,
                        "voice": voice,
                        "speed": speed,
                    },
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Kokoro Provider Runtime unavailable at {tts_lab_base}: {exc}",
                ) from exc
            if response.is_error:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            return Response(
                content=response.content,
                media_type="audio/wav",
                headers={
                    "X-Media-Provider": "kokoro",
                    "X-Media-Voice": response.headers.get("x-tts-voice", voice),
                    "X-Media-Device": response.headers.get("x-tts-device", settings.tts_device),
                    "X-Media-Inference-Ms": response.headers.get("x-tts-inference-ms", "0"),
                    "X-Media-Audio-Ms": response.headers.get("x-tts-audio-ms", "0"),
                    "X-Media-Sample-Rate": response.headers.get("x-tts-sample-rate", "24000"),
                },
            )

        try:
            if configured_routing:
                configured_voice = str(settings.tts_voice or "0")
                voice_value = explicit_voice or configured_voice
                speaker_id = int(voice_value) if voice_value.isdigit() else 0
                speed = float(req.speed if explicit_voice and req.speed is not None else settings.tts_speed)
            else:
                speaker_id = int(req.speaker_id or 0)
                speed = float(req.speed if req.speed is not None else 1.0)
            result = media.synthesize(req.text, speaker_id=speaker_id, speed=speed)
        except (RuntimeError, ValueError) as exc:
            code = 503 if isinstance(exc, RuntimeError) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-Media-Provider": result.provider,
                "X-Media-Voice": str(speaker_id),
                "X-Media-Device": result.device,
                "X-Media-Inference-Ms": str(result.inference_ms),
                "X-Media-Audio-Ms": str(result.audio_ms),
                "X-Media-Sample-Rate": str(result.sample_rate),
            },
        )

    @app.get("/v1/metrics/recent")
    def recent_metrics(limit: int = Query(default=50, ge=1, le=200)):
        return {"metrics": media.recent_metrics(limit)}

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Media Runtime requires uvicorn: pip install -e '.[api,media]'") from exc
    host = os.getenv("CHARACTER_MEDIA_HOST", "127.0.0.1")
    port = int(os.getenv("CHARACTER_MEDIA_PORT", "8001"))
    print(f"media: http://{host}:{port}")
    uvicorn.run(create_media_app(), host=host, port=port, reload=False, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
