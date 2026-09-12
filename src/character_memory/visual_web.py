from __future__ import annotations

import base64
from datetime import datetime
import logging
from pathlib import Path
import time

from pydantic import BaseModel, Field, model_validator

from character_memory.config import load_persona
from character_memory.domain.models import EventType
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    build_image_providers,
)


logger = logging.getLogger("character_memory.visual_web")


class AvatarGenerateRequest(BaseModel):
    provider: str = Field(default="", max_length=32)
    hint: str = Field(default="", max_length=600)


class AvatarFromChatRequest(BaseModel):
    media_id: str | None = Field(default=None, max_length=80)
    image_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def exactly_one_source(self):
        if bool(self.media_id) == bool(self.image_id):
            raise ValueError("provide exactly one of media_id or image_id")
        return self


def attach_visual_routes(app) -> None:
    """Attach direct-character visual tooling without exposing a chat ImageGen console."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before visual routes are attached")
    avatar_store = getattr(access, "avatar_store", None)
    if avatar_store is None:
        raise RuntimeError("attach_avatar_routes() must run before attach_visual_routes()")

    settings = access.settings
    providers = build_image_providers(settings)
    access.image_generation_providers = providers

    def profile(character_id: str) -> dict[str, str]:
        for item in access.character_profiles():
            if item["id"] == character_id:
                return item
        raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")

    def avatar_url(character_id: str) -> str:
        version = avatar_store.version(character_id)
        return f"/v1/characters/{character_id}/avatar/asset?v={version}" if version else ""

    def recent_dialogue(character_id: str) -> list[str]:
        try:
            events = access.read_store.list_chat_events(character_id, limit=8)
        except Exception:
            logger.exception("visual recent_dialogue failed character=%s", character_id)
            return []
        lines: list[str] = []
        for event in events[-8:]:
            role = "用户" if event.event_type == EventType.USER_MESSAGE else "角色"
            content = event.metadata.get("display_text", event.content) if role == "用户" else event.content
            text = " ".join(str(content or "").split()).strip()
            if text:
                lines.append(f"{role}: {text[:320]}")
        return lines

    def current_avatar_data_url(character_id: str) -> str | None:
        path = avatar_store.asset_path(character_id)
        metadata = avatar_store.load(character_id)
        if path is None or metadata is None:
            return None
        payload = path.read_bytes()
        return f"data:{metadata.content_type};base64,{base64.b64encode(payload).decode('ascii')}"

    def provider_for(name: str):
        provider_name = (name or getattr(settings, "image_generation_provider", "agnes") or "agnes").strip().lower()
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Unknown image provider: {provider_name}")
        if not provider.available():
            key_hint = "AGNES_API_KEY" if provider_name == "agnes" else "MSIMG_API_KEY / MODELSCOPE_API_TOKEN"
            raise HTTPException(status_code=503, detail=f"{provider_name} is not configured; set {key_hint}")
        return provider_name, provider

    @app.get("/v1/visual/providers")
    def visual_providers():
        return {
            "default": str(getattr(settings, "image_generation_provider", "agnes") or "agnes"),
            "providers": [
                {
                    "id": name,
                    "configured": provider.available(),
                    "supports_reference_images": bool(provider.supports_reference_images),
                    "model": str(getattr(provider, "model", "") or (getattr(provider, "models", [""])[0])),
                }
                for name, provider in providers.items()
            ],
        }

    @app.post("/v1/characters/{character_id}/avatar/generate")
    def generate_avatar_candidate(character_id: str, req: AvatarGenerateRequest):
        item = profile(character_id)
        provider_name, provider = provider_for(req.provider)
        started = time.perf_counter()
        current = access.require_bundle()
        runtime = getattr(current, "runtimes", {}).get(character_id)
        persona = getattr(runtime, "persona", "") if runtime is not None else ""
        if not persona:
            persona = load_persona(item["persona_path"])
        mental_state = access.read_store.get_mental_state(character_id)
        reference = current_avatar_data_url(character_id) if provider.supports_reference_images else None
        try:
            plan = VisualPromptPlanner(current.model).plan(
                character_id,
                purpose=VisualPurpose.AVATAR,
                persona=persona,
                mental_state=mental_state,
                recent_dialogue=recent_dialogue(character_id),
                visual_intent=req.hint.strip() or "生成一张保持人物核心身份、适合作为当前聊天头像的自然头像",
                has_reference_image=bool(reference),
            )
            result = provider.generate(
                ImageGenerationRequest(
                    prompt=plan.positive_prompt,
                    negative_prompt=plan.negative_prompt,
                    aspect_ratio="1:1",
                    size="1K",
                    reference_images=[reference] if reference else [],
                )
            )
            asset = access.media_storage.save_bytes(
                character_id=character_id,
                original_name=f"generated-avatar-{provider_name}.png",
                payload=result.payload,
                created_at=datetime.now().astimezone(),
                source="GENERATED_AVATAR_CANDIDATE",
            )
            access.store().add_media_asset(asset)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("visual.avatar generate_failed character=%s provider=%s error=%s", character_id, provider_name, exc)
            raise HTTPException(status_code=502, detail=f"头像生成失败：{exc}") from exc
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "visual.avatar generated character=%s provider=%s model=%s media=%s duration_ms=%.1f",
            character_id,
            result.provider,
            result.model,
            asset.id,
            elapsed,
        )
        return {
            "character_id": character_id,
            "candidate": {
                "media_id": asset.id,
                "url": f"/v1/media/{asset.id}",
                "source": asset.source,
                "provider": result.provider,
                "model": result.model,
                "supports_reference_images": bool(provider.supports_reference_images),
            },
            "visual_intent": plan.visual_intent,
            "aspect_ratio": plan.aspect_ratio,
            "duration_ms": elapsed,
        }

    @app.post("/v1/characters/{character_id}/avatar/from-chat")
    def avatar_from_chat(character_id: str, req: AvatarFromChatRequest):
        item = profile(character_id)
        try:
            if req.media_id:
                asset = access.read_store.get_media_asset(req.media_id)
                if asset is None:
                    raise HTTPException(status_code=404, detail="chat media not found")
                if asset.character_id != character_id:
                    raise HTTPException(status_code=400, detail="chat media belongs to another conversation/character")
                path = access.media_storage.asset_path(asset)
                if path is None:
                    raise HTTPException(status_code=404, detail="chat media file not found")
                metadata = avatar_store.save_from_path(
                    character_id,
                    path,
                    source="CHAT_ASSET" if not asset.source.startswith("GENERATED") else "GENERATED",
                    source_media_id=asset.id,
                    title=asset.original_name,
                )
            else:
                current = access.require_bundle()
                runtime = getattr(current, "runtimes", {}).get(character_id)
                catalog = getattr(runtime, "image_catalog", None) if runtime is not None else None
                if catalog is None:
                    from character_memory.images import load_image_catalog
                    catalog = load_image_catalog(item["persona_path"])
                image = catalog.get(req.image_id) if catalog is not None else None
                path = catalog.asset_path(req.image_id) if image is not None else None
                if image is None or path is None:
                    raise HTTPException(status_code=404, detail="character image not found")
                metadata = avatar_store.save_from_path(
                    character_id,
                    path,
                    source="CHAT_ASSET",
                    title=image.label,
                )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("visual.avatar from_chat_failed character=%s error=%s", character_id, exc)
            raise HTTPException(status_code=500, detail=f"设置头像失败：{exc}") from exc
        return {
            "character_id": character_id,
            "avatar_url": avatar_url(character_id),
            "avatar": metadata.model_dump(mode="json"),
        }

    @app.on_event("shutdown")
    def _close_visual_providers():
        for provider in providers.values():
            try:
                provider.close()
            except Exception:
                logger.exception("visual provider close_failed provider=%s", getattr(provider, "name", "unknown"))
