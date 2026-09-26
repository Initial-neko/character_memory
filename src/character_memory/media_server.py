from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.config import DEFAULT_VOICE_SILENCE_MS, load_settings
from character_memory.media_runtime import MediaRuntime, build_media_runtime_from_env
from character_memory.tts_registry import FORMAL_TTS_PROVIDER_SET, provider_spec


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # Legacy Sherpa request fields stay accepted. In production configured routing,
    # formal defaults are authoritative unless `voice` is explicitly supplied.
    speaker_id: int | None = Field(default=None, ge=0, le=10000)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    voice: str | None = Field(default=None, max_length=128)


def create_media_app(runtime: MediaRuntime | None = None, *, provider_http_client=None):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import Response
        from starlette.websockets import WebSocket, WebSocketDisconnect
    except ImportError as exc:
        raise RuntimeError("Media Runtime requires the api extra: pip install -e '.[api]'") from exc

    # Explicit runtime injection is the provider-neutral test/embedding contract.
    # Production startup passes no runtime and therefore uses configured routing.
    configured_routing = runtime is None
    media = runtime or build_media_runtime_from_env()
    config_path = os.getenv("CHARACTER_CONFIG_PATH", os.getenv("CHARACTER_MEMORY_CONFIG", "config.yaml"))
    def current_settings():
        return load_settings(config_path)
    tts_lab_base = os.getenv("CHARACTER_TTS_LAB_BASE", "http://127.0.0.1:9002").rstrip("/")
    owns_provider_client = provider_http_client is None
    provider_client = provider_http_client or httpx.Client(timeout=180.0)

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

    @on_app_event(app, "shutdown")
    def shutdown():
        if owns_provider_client:
            provider_client.close()

    def selected_tts_status(runtime_status: dict) -> dict:
        local_tts = dict(runtime_status.get("tts") or {})
        if not configured_routing:
            return {
                **local_tts,
                "route": "injected-runtime",
                "voice": None,
                "speed": None,
                "restart_required_for_config_changes": False,
            }

        settings = current_settings()
        selected = str(settings.tts_provider or "sherpa").strip().lower()
        base = {
            "provider": selected,
            "voice": settings.tts_voice,
            "speed": settings.tts_speed,
            "device": settings.tts_device,
            "restart_required_for_config_changes": False,
        }
        if selected not in FORMAL_TTS_PROVIDER_SET:
            return {**base, "ready": False, "loaded": False, "reason": f"Unknown TTS provider: {selected}"}

        if selected == "sherpa":
            actual_device = str(local_tts.get("device") or "").strip().lower()
            configured_device = str(settings.tts_device or "").strip().lower()
            return {
                **base,
                **local_tts,
                "provider": "sherpa",
                "voice": settings.tts_voice,
                "speed": settings.tts_speed,
                "configured_device": configured_device,
                "restart_required_for_config_changes": bool(
                    actual_device and configured_device and actual_device != configured_device
                ),
            }

        # Query only the selected provider. Calling :9002/health would also ask
        # its Sherpa adapter to call this Media Runtime and create a health cycle.
        # Keep this probe shorter than the stack's normal 0.8s liveness timeout.
        provider_id = selected
        try:
            response = provider_client.get(f"{tts_lab_base}/v1/providers/{provider_id}", timeout=0.4)
            response.raise_for_status()
            provider = dict((response.json() or {}).get("provider") or {})
            actual_device = str(provider.get("device") or "").strip().lower()
            configured_device = str(settings.tts_device or "").strip().lower()
            spec = provider_spec(provider_id)
            return {
                **base,
                **provider,
                "provider": provider_id,
                "voice": settings.tts_voice,
                "speed": settings.tts_speed,
                "device": provider.get("device") or ("cloud" if provider_id == "edge" else settings.tts_device),
                "configured_device": configured_device,
                "restart_required_for_config_changes": bool(
                    spec.device_mode == "local"
                    and not spec.device_hot_apply
                    and actual_device
                    and configured_device
                    and not actual_device.startswith(configured_device)
                ),
            }
        except Exception as exc:
            return {
                **base,
                "ready": False,
                "loaded": False,
                "reason": f"{provider_id} Provider Runtime unavailable at {tts_lab_base}: {exc}",
            }

    @app.get("/health")
    def health():
        status = media.status()
        runtime_tts = dict(status.get("tts") or {})
        selected_tts = selected_tts_status(status)
        # `tts` remains the browser-facing contract and now reflects the route
        # that /v1/tts will actually use. Keep the underlying Sherpa runtime
        # separately so the TTS Lab can still inspect/audition it.
        status["tts_runtime"] = runtime_tts
        status["tts"] = selected_tts
        status["tts_selected"] = selected_tts
        # Voice-call capture parameters. The browser already fetches /health
        # before it opens the microphone, so serving them here costs no extra
        # round trip and keeps one source of truth for the endpointing
        # threshold. getattr, because several callers stub load_settings with a
        # bare namespace that predates this field.
        status["voice_capture"] = {
            "silence_ms": int(
                getattr(current_settings(), "voice_silence_ms", DEFAULT_VOICE_SILENCE_MS)
            ),
        }
        return {"ok": True, **status}

    @app.websocket("/v1/asr/stream")
    async def stream_asr(websocket: WebSocket):
        """Binary PCM16 streaming endpoint.

        Client protocol:
        - first text frame: {"op":"start","source":"call|dictation"}
        - binary frames: mono PCM16, 16 kHz
        - optional text frame: {"op":"flush","reason":"user_stop"}
        - optional text frame: {"op":"cancel"}
        - server text frames: partial/final/error JSON events

        Only FINAL is suitable for Character Runtime submission.
        """
        await websocket.accept()
        asr = media.asr
        create_session = getattr(asr, "create_session", None)
        if create_session is None:
            await websocket.send_json({"kind": "error", "code": "streaming_unavailable"})
            await websocket.close(code=1011)
            return

        session = None
        session_id = None
        segment_id = 1
        try:
            first = await websocket.receive_json()
            if first.get("op") != "start":
                raise ValueError("first frame must be start")
            session = create_session()
            session_id = f"media-{id(session)}"
            await websocket.send_json({
                "kind": "ready",
                "session_id": session_id,
                "segment_id": segment_id,
                "provider": asr.status().get("provider"),
            })

            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    payload = message["bytes"]
                    if len(payload) % 2:
                        raise ValueError("PCM16 frame has odd byte length")
                    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
                    text = session.push_audio(samples, sample_rate=16000)
                    if text:
                        await websocket.send_json({
                            "kind": "partial",
                            "session_id": session_id,
                            "segment_id": segment_id,
                            "revision": 0,
                            "text": text,
                        })
                    if session.is_endpoint():
                        result = session.finish()
                        if result.text:
                            await websocket.send_json({
                                "kind": "final",
                                "session_id": session_id,
                                "segment_id": segment_id,
                                "revision": 1,
                                "text": result.text,
                                "endpoint_reason": "model_endpoint",
                            })
                        segment_id += 1
                        session = create_session()
                    continue

                text_message = message.get("text")
                if text_message is None:
                    continue
                command = __import__("json").loads(text_message)
                op = command.get("op")
                if op in {"flush", "finish", "stop"}:
                    reason = str(command.get("reason") or "user_stop")
                    result = session.finish()
                    if result.text:
                        await websocket.send_json({
                            "kind": "final",
                            "session_id": session_id,
                            "segment_id": segment_id,
                            "revision": 1,
                            "text": result.text,
                            "endpoint_reason": reason,
                        })
                    segment_id += 1
                    session = create_session()
                    continue
                if op == "cancel":
                    break
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await websocket.send_json({
                "kind": "error",
                "session_id": session_id,
                "segment_id": segment_id,
                "code": "stream_failed",
                "detail": str(exc),
            })
        finally:
            if session is not None:
                try:
                    session.finish()
                except Exception:
                    pass

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

    def _local_sherpa_response(req: TtsRequest, *, speaker_id: int, speed: float):
        try:
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

    @app.post("/v1/providers/sherpa/tts")
    def synthesize_sherpa_provider(req: TtsRequest):
        """Provider-specific Sherpa endpoint used by the TTS Workbench.

        Unlike /v1/tts this route never consults the configured formal provider,
        so auditioning Sherpa cannot accidentally route to Kokoro/GSV/Edge.
        """

        explicit_voice = str(req.voice or "").strip()
        speaker_id = int(explicit_voice) if explicit_voice.isdigit() else int(req.speaker_id or 0)
        speed = float(req.speed if req.speed is not None else 1.0)
        return _local_sherpa_response(req, speaker_id=speaker_id, speed=speed)

    @app.post("/v1/tts")
    def synthesize(req: TtsRequest):
        settings = current_settings()
        selected = str(settings.tts_provider or "sherpa").strip().lower()
        explicit_voice = str(req.voice or "").strip()

        if configured_routing and selected not in FORMAL_TTS_PROVIDER_SET:
            raise HTTPException(status_code=400, detail=f"Unknown TTS provider: {selected}")

        if configured_routing and selected in {"kokoro", "edge", "gsv"}:
            # :9002 is both the audition UI and the provider service in V1.
            # Media Runtime remains the stable browser-facing endpoint.
            default_voice = provider_spec(selected).default_voice
            voice = explicit_voice or str(settings.tts_voice or default_voice)
            speed = float(req.speed if explicit_voice and req.speed is not None else settings.tts_speed)
            try:
                response = provider_client.post(
                    f"{tts_lab_base}/v1/tts",
                    json={"provider": selected, "text": req.text, "voice": voice, "speed": speed},
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"{selected} Provider Runtime unavailable at {tts_lab_base}: {exc}",
                ) from exc
            if response.is_error:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            fallback_media_type = "audio/mpeg" if selected == "edge" else "audio/wav"
            media_type = (response.headers.get("content-type") or fallback_media_type).split(";", 1)[0]
            return Response(
                content=response.content,
                media_type=media_type,
                headers={
                    "X-Media-Provider": selected,
                    "X-Media-Voice": response.headers.get("x-tts-voice", voice),
                    "X-Media-Device": response.headers.get("x-tts-device", "cloud" if selected == "edge" else settings.tts_device),
                    "X-Media-Inference-Ms": response.headers.get("x-tts-inference-ms", "0"),
                    "X-Media-Audio-Ms": response.headers.get("x-tts-audio-ms", "0"),
                    "X-Media-Sample-Rate": response.headers.get("x-tts-sample-rate", "32000" if selected == "gsv" else "24000"),
                },
            )

        if configured_routing:
            configured_voice = str(settings.tts_voice or "0")
            # `voice` carries a character id (`"momo"`), not a speaker number, so a
            # non-numeric voice falls through to the browser-provided speaker hash
            # before the configured numeric Sherpa default.
            if explicit_voice.isdigit():
                speaker_id = int(explicit_voice)
            elif req.speaker_id is not None:
                speaker_id = req.speaker_id
            elif configured_voice.isdigit():
                speaker_id = int(configured_voice)
            else:
                speaker_id = 0
            speed = float(req.speed if explicit_voice and req.speed is not None else settings.tts_speed)
        else:
            speaker_id = int(req.speaker_id or 0)
            speed = float(req.speed if req.speed is not None else 1.0)
        return _local_sherpa_response(req, speaker_id=speaker_id, speed=speed)

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
