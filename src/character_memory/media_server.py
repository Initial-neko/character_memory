from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

from character_memory.config import load_settings
from character_memory.media_runtime import MediaRuntime, build_media_runtime_from_env


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
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

    @app.get("/health")
    def health():
        status = media.status()
        status["tts_selected"] = {
            "provider": settings.tts_provider if configured_routing else "injected-runtime",
            "voice": settings.tts_voice if configured_routing else None,
            "speed": settings.tts_speed if configured_routing else None,
            "device": settings.tts_device if configured_routing else None,
            "restart_required_for_config_changes": configured_routing,
        }
        return {"ok": True, **status}

    @app.post("/v1/asr")
    async def transcribe(
        payload: bytes = Body(..., media_type="application/octet-stream"),
        content_type: str | None = Header(default=None, alias="Content-Type"),
    ):
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
        if configured_routing and selected == "kokoro":
            # :9002 is both the audition UI and the local provider service in V1.
            # Media Runtime remains the stable browser-facing TTS endpoint, so the
            # chat page does not need provider-specific URLs or CORS rules.
            voice = str(req.voice or settings.tts_voice or "zf_001")
            speed = float(req.speed if req.speed is not None else settings.tts_speed)
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
                speaker_id = int(settings.tts_voice) if str(settings.tts_voice).isdigit() else 0
                if req.speaker_id is not None:
                    speaker_id = req.speaker_id
                speed = float(req.speed if req.speed is not None else settings.tts_speed)
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
