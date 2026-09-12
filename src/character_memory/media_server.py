from __future__ import annotations

import os

from pydantic import BaseModel, Field

from character_memory.media_runtime import MediaRuntime, build_media_runtime_from_env


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    speaker_id: int = Field(default=0, ge=0, le=10000)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def create_media_app(runtime: MediaRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Query, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError("Media Runtime requires the api extra: pip install -e '.[api]'") from exc

    media = runtime or build_media_runtime_from_env()
    app = FastAPI(title="Character Memory Media Runtime", version="0.1")
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

    @app.get("/health")
    def health():
        return {"ok": True, **media.status()}

    @app.post("/v1/asr")
    async def transcribe(request: Request):
        content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Voice V0 accepts audio/wav PCM16")
        payload = await request.body()
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
        try:
            result = media.synthesize(req.text, speaker_id=req.speaker_id, speed=req.speed)
        except (RuntimeError, ValueError) as exc:
            code = 503 if isinstance(exc, RuntimeError) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-Media-Provider": result.provider,
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
