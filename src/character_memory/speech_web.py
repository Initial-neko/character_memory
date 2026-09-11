from __future__ import annotations

import logging
import threading

from character_memory.speech import create_speech_provider


logger = logging.getLogger("character_memory.speech_web")


def _dedupe_hotwords(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= 64:
            break
    return result


def attach_speech_routes(app) -> None:
    """Attach draft-only ASR routes.

    This module never calls chat submission. The browser receives plain draft
    text and the existing composer remains the only send owner.
    """
    from fastapi import HTTPException, Request

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before speech routes are attached")

    settings = access.settings
    provider_lock = threading.Lock()
    provider_holder: dict[str, object] = {}

    def get_provider():
        provider = provider_holder.get("provider")
        if provider is not None:
            return provider
        with provider_lock:
            provider = provider_holder.get("provider")
            if provider is None:
                provider = create_speech_provider(settings)
                provider_holder["provider"] = provider
            return provider

    def hotwords_for(character_id: str | None) -> list[str]:
        profiles = list(access.character_profiles())
        selected = []
        others = []
        for profile in profiles:
            name = str(profile.get("name") or "").strip()
            if not name:
                continue
            if character_id and profile.get("id") == character_id:
                selected.append(name)
            else:
                others.append(name)
        configured = list(getattr(settings, "asr_hotwords", []) or [])
        return _dedupe_hotwords([*selected, *others, *configured])

    @app.get("/v1/speech/status")
    def speech_status():
        provider_name = str(getattr(settings, "asr_provider", "funasr") or "funasr").strip().lower()
        return {
            "enabled": provider_name not in {"", "none", "disabled", "off"},
            "provider": provider_name,
            "model": str(getattr(settings, "asr_model", "")),
            "device": str(getattr(settings, "asr_device", "")),
            "language": str(getattr(settings, "asr_language", "中文")),
            "max_bytes": int(getattr(settings, "asr_max_bytes", 24 * 1024 * 1024)),
        }

    @app.post("/v1/speech/transcribe")
    async def transcribe_speech(request: Request, character_id: str | None = None):
        audio = await request.body()
        max_bytes = int(getattr(settings, "asr_max_bytes", 24 * 1024 * 1024))
        if not audio:
            raise HTTPException(status_code=400, detail="录音为空")
        if len(audio) > max_bytes:
            raise HTTPException(status_code=413, detail=f"录音过大，最大允许 {max_bytes // (1024 * 1024)} MiB")

        media_type = request.headers.get("content-type", "audio/webm")
        language = str(getattr(settings, "asr_language", "中文") or "中文")
        hotwords = hotwords_for(character_id)
        try:
            result = get_provider().transcribe(
                audio,
                media_type=media_type,
                language=language,
                hotwords=hotwords,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("speech.transcribe failed provider=%s error=%s", getattr(settings, "asr_provider", ""), exc)
            raise HTTPException(status_code=503, detail=f"本地语音识别失败：{exc}") from exc

        logger.info(
            "speech.transcribe done provider=%s model=%s bytes=%d duration_ms=%.1f",
            result.provider,
            result.model,
            len(audio),
            result.transcription_ms,
        )
        return {
            **result.to_dict(),
            "hotword_count": len(hotwords),
        }
