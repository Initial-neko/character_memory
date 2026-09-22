from __future__ import annotations

import base64
from datetime import datetime
import logging
import time

from pydantic import BaseModel, Field, model_validator

from character_memory.config import load_persona
from character_memory.domain.models import EventType
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)
from character_memory.visual_runtime import configure_direct_visual_runtime


logger = logging.getLogger("character_memory.visual_web")


_AVATAR_STYLE_GUIDANCE = {
    "AUTO": "根据 Persona 和角色世界观自动选择最契合、可长期保持一致的头像画风；不要为了候选差异改变人物身份。",
    "ANIME_CLEAN": "清爽二次元角色插画风；线条干净，面部清晰，色彩克制，背景简洁，适合作为聊天头像。",
    "SOFT_ILLUSTRATION": "柔和半写实插画风；自然光影与细腻五官，保留角色辨识度，不做摄影棚海报感。",
    "NATURAL_PORTRAIT": "自然人像风；真实但不过度照片化，构图像日常社交头像，背景简单，避免证件照和商业棚拍。",
}

_AVATAR_VARIATIONS = (
    "正面或轻微三分之四视角，神情自然稳定。",
    "轻微三分之四视角，表情柔和，保持相同服装与核心视觉身份。",
    "更靠近头像构图，神情有一点角色个性，但不要夸张。",
    "构图稍有变化，仍保持同一人物、同一画风与相同核心外观。",
)


class AvatarGenerateRequest(BaseModel):
    provider: str = Field(default="", max_length=32)
    hint: str = Field(default="", max_length=600)
    style: str = Field(default="AUTO", max_length=32)
    count: int = Field(default=4, ge=1, le=4)

    @model_validator(mode="after")
    def supported_style(self):
        self.style = str(self.style or "AUTO").strip().upper()
        if self.style not in _AVATAR_STYLE_GUIDANCE:
            raise ValueError(
                "avatar style must be one of: "
                + ", ".join(_AVATAR_STYLE_GUIDANCE)
            )
        self.hint = self.hint.strip()
        return self


class AvatarFromChatRequest(BaseModel):
    media_id: str | None = Field(default=None, max_length=80)
    image_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def exactly_one_source(self):
        if bool(self.media_id) == bool(self.image_id):
            raise ValueError("provide exactly one of media_id or image_id")
        return self


class VisualTestRequest(BaseModel):
    character_id: str = Field(default="rin", min_length=1, max_length=64)
    provider: str = Field(default="", max_length=32)
    purpose: VisualPurpose = VisualPurpose.SELFIE
    visual_intent: str = Field(default="自然分享一下现在的样子", min_length=1, max_length=800)
    use_avatar_reference: bool = True

    @model_validator(mode="after")
    def supported_purpose(self):
        if self.purpose not in {VisualPurpose.AVATAR, VisualPurpose.SELFIE, VisualPurpose.SCENE}:
            raise ValueError("Dev visual test purpose must be AVATAR, SELFIE or SCENE")
        return self


class ImageRewriteRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=1600)
    provider: str = Field(default="", max_length=32)
    purpose: VisualPurpose = VisualPurpose.SCENE
    use_avatar_reference: bool = True

    @model_validator(mode="after")
    def supported_purpose(self):
        if self.purpose not in {VisualPurpose.AVATAR, VisualPurpose.SELFIE, VisualPurpose.SCENE}:
            raise ValueError("image purpose must be AVATAR, SELFIE or SCENE")
        self.instruction = self.instruction.strip()
        if not self.instruction:
            raise ValueError("instruction must not be empty")
        return self


class ImageGenerateRequest(ImageRewriteRequest):
    # Chat generation keeps the result in-memory until the user presses Send, so
    # generated drafts behave exactly like pasted images and do not pollute chat
    # history. Dev Console may persist a candidate for inspection/avatar testing.
    persist_result: bool = False


def attach_visual_routes(app) -> None:
    """Attach direct-character visual capability, explicit user tools and Dev smoke routes."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before visual routes are attached")
    services = access.services
    avatar_store = services.avatar_store
    settings = access.settings
    providers = services.image_generation_providers
    configure_direct_visual_runtime(access)

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

    def provider_named(name: str):
        provider_name = (name or getattr(settings, "image_generation_provider", "agnes") or "agnes").strip().lower()
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Unknown image provider: {provider_name}")
        return provider_name, provider

    def provider_for(name: str):
        provider_name, provider = provider_named(name)
        configured = provider.configured() if hasattr(provider, "configured") else provider.available()
        if not configured:
            key_hint = "AGNES_API_KEY" if provider_name == "agnes" else "MSIMG_API_KEY / MODELSCOPE_API_TOKEN"
            raise HTTPException(status_code=503, detail=f"{provider_name} is not configured; set {key_hint}")
        if not provider.available():
            if provider_name == "msimg":
                raise HTTPException(status_code=503, detail="msimg is configured but unavailable; run bash scripts/sync-all.sh")
            raise HTTPException(status_code=503, detail=f"{provider_name} is configured but unavailable")
        return provider_name, provider

    def provider_status(name: str, provider) -> dict:
        configured = provider.configured() if hasattr(provider, "configured") else provider.available()
        available = provider.available()
        model = str(getattr(provider, "model", "") or "")
        if not model:
            models = getattr(provider, "models", []) or []
            model = ",".join(str(value) for value in models)
        reason = ""
        if not configured:
            reason = "missing_api_key"
        elif not available:
            reason = "missing_dependency_or_runtime"
        return {
            "id": name,
            "configured": bool(configured),
            "available": bool(available),
            "reason": reason,
            "supports_reference_images": bool(provider.supports_reference_images),
            "model": model,
        }

    def runtime_persona(character_id: str, item: dict[str, str]):
        current = access.require_bundle()
        runtime = getattr(current, "runtimes", {}).get(character_id)
        persona = getattr(runtime, "persona", "") if runtime is not None else ""
        if not persona:
            persona = load_persona(item["persona_path"])
        return current, runtime, persona

    def compile_instruction(character_id: str, req: ImageRewriteRequest, *, provider=None):
        item = profile(character_id)
        current, _runtime, persona = runtime_persona(character_id, item)
        mental_state = access.read_store.get_mental_state(character_id)
        reference = None
        if req.use_avatar_reference and (provider is None or provider.supports_reference_images):
            reference = current_avatar_data_url(character_id)
        prompt = VisualPromptPlanner(current.model).compile_prompt(
            character_id,
            purpose=req.purpose,
            persona=persona,
            mental_state=mental_state,
            recent_dialogue=recent_dialogue(character_id),
            visual_intent=req.instruction,
            has_reference_image=bool(reference),
        )
        return current, prompt, reference, visual_aspect_ratio(req.purpose)

    def generated_payload_bytes(payload: bytes) -> tuple[bytes, str, str]:
        data = bytes(payload or b"")
        if not data:
            raise ValueError("generated image is empty")
        max_bytes = int(getattr(access.media_storage, "max_bytes", 8 * 1024 * 1024))
        if len(data) > max_bytes:
            raise ValueError(f"generated image exceeds {max_bytes // (1024 * 1024)} MiB limit")
        mime = access.media_storage._sniff_mime(data)
        if mime is None:
            raise ValueError("generated image format is unsupported")
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }[mime]
        return data, mime, extension

    @app.get("/v1/visual/providers")
    def visual_providers():
        return {
            "default": str(getattr(settings, "image_generation_provider", "agnes") or "agnes"),
            "providers": [provider_status(name, provider) for name, provider in providers.items()],
        }

    @app.post("/v1/characters/{character_id}/images/rewrite")
    def rewrite_user_image_prompt(character_id: str, req: ImageRewriteRequest):
        """Polish a natural-language drawing instruction without generating an image."""
        provider_name, provider = provider_named(req.provider)
        started = time.perf_counter()
        try:
            _current, prompt, reference, aspect_ratio = compile_instruction(character_id, req, provider=provider)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("visual.rewrite failed character=%s purpose=%s error=%s", character_id, req.purpose.value, exc)
            raise HTTPException(status_code=502, detail=f"图片 Prompt 润色失败：{exc}") from exc
        return {
            "ok": True,
            "character_id": character_id,
            "purpose": req.purpose.value,
            "provider": provider_name,
            "instruction": req.instruction,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "used_avatar_reference": bool(reference),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @app.post("/v1/characters/{character_id}/images/generate")
    def generate_user_image(character_id: str, req: ImageGenerateRequest):
        """Rewrite an instruction with AI, generate it, and return a sendable image draft.

        By default no chat event or MediaAsset is created. The browser receives a
        data URL and feeds it into the existing image-draft flow; persistence only
        happens after the user explicitly presses Send, just like clipboard paste.
        """
        provider_name, provider = provider_for(req.provider)
        started = time.perf_counter()
        try:
            _current, prompt, reference, aspect_ratio = compile_instruction(character_id, req, provider=provider)
            result = provider.generate(
                ImageGenerationRequest(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    size="1K",
                    reference_images=[reference] if reference else [],
                )
            )
            payload, mime_type, extension = generated_payload_bytes(result.payload)
            filename = f"ai-generated-{req.purpose.value.lower()}.{extension}"
            data_url = f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"
            asset = None
            if req.persist_result:
                asset = access.media_storage.save_bytes(
                    character_id=character_id,
                    original_name=filename,
                    payload=payload,
                    created_at=datetime.now().astimezone(),
                    source=f"TOOL_GENERATED_{req.purpose.value}",
                )
                access.store().add_media_asset(asset)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("visual.user_generate failed character=%s provider=%s purpose=%s error=%s", character_id, provider_name, req.purpose.value, exc)
            raise HTTPException(status_code=502, detail=f"AI 图片生成失败：{exc}") from exc

        elapsed = round((time.perf_counter() - started) * 1000, 1)
        image = {
            "filename": filename,
            "data_url": data_url,
            "mime_type": mime_type,
            "size_bytes": len(payload),
            "media_id": asset.id if asset is not None else None,
            "url": f"/v1/media/{asset.id}" if asset is not None else "",
            "source": asset.source if asset is not None else "AI_GENERATED_DRAFT",
        }
        logger.info(
            "visual.user_generate done character=%s provider=%s model=%s purpose=%s persisted=%s duration_ms=%.1f",
            character_id,
            result.provider,
            result.model,
            req.purpose.value,
            bool(asset),
            elapsed,
        )
        return {
            "ok": True,
            "character_id": character_id,
            "purpose": req.purpose.value,
            "provider": result.provider,
            "model": result.model,
            "instruction": req.instruction,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "used_avatar_reference": bool(reference),
            "duration_ms": elapsed,
            "image": image,
        }

    @app.post("/v1/visual/test")
    def visual_test(req: VisualTestRequest):
        """Real plain-text prompt planner + provider + MediaStorage smoke test."""
        item = profile(req.character_id)
        provider_name, provider = provider_for(req.provider)
        started = time.perf_counter()
        current, _runtime, persona = runtime_persona(req.character_id, item)
        mental_state = access.read_store.get_mental_state(req.character_id)
        reference = None
        if req.use_avatar_reference and req.purpose in {VisualPurpose.AVATAR, VisualPurpose.SELFIE} and provider.supports_reference_images:
            reference = current_avatar_data_url(req.character_id)
        try:
            prompt = VisualPromptPlanner(current.model).compile_prompt(
                req.character_id,
                purpose=req.purpose,
                persona=persona,
                mental_state=mental_state,
                recent_dialogue=recent_dialogue(req.character_id),
                visual_intent=req.visual_intent.strip(),
                has_reference_image=bool(reference),
            )
            aspect_ratio = visual_aspect_ratio(req.purpose)
            result = provider.generate(
                ImageGenerationRequest(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    size="1K",
                    reference_images=[reference] if reference else [],
                )
            )
            suffix = result.mime_type.split("/", 1)[-1] or "png"
            asset = access.media_storage.save_bytes(
                character_id=req.character_id,
                original_name=f"dev-{req.purpose.value.lower()}-{provider_name}.{suffix}",
                payload=result.payload,
                created_at=datetime.now().astimezone(),
                source=f"DEV_GENERATED_{req.purpose.value}",
            )
            access.store().add_media_asset(asset)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("visual.test failed character=%s provider=%s purpose=%s error=%s", req.character_id, provider_name, req.purpose.value, exc)
            raise HTTPException(status_code=502, detail=f"ImageGen 测试失败：{exc}") from exc

        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": True,
            "character_id": req.character_id,
            "purpose": req.purpose.value,
            "provider": result.provider,
            "model": result.model,
            "duration_ms": elapsed,
            "used_avatar_reference": bool(reference),
            "plan": {
                "visual_intent": req.visual_intent.strip(),
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
            },
            "image": {
                "media_id": asset.id,
                "url": f"/v1/media/{asset.id}",
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "source": asset.source,
            },
        }

    @app.post("/v1/characters/{character_id}/avatar/generate")
    def generate_avatar_candidate(character_id: str, req: AvatarGenerateRequest):
        profile(character_id)
        provider_name, provider = provider_for(req.provider)
        started = time.perf_counter()
        style_guidance = _AVATAR_STYLE_GUIDANCE[req.style]
        base_intent = (
            req.hint
            or "保持人物核心身份，生成适合作为当前聊天头像的自然候选"
        )
        visual_intent = (
            f"{base_intent}\n"
            f"统一头像画风要求：{style_guidance}\n"
            "候选之间只允许构图、视角和细微表情变化；"
            "人物身份、年龄感、发色、眼睛、服装基调与整体画风必须保持一致。"
        )
        rewrite_req = ImageRewriteRequest(
            instruction=visual_intent,
            provider=provider_name,
            purpose=VisualPurpose.AVATAR,
            use_avatar_reference=True,
        )
        try:
            # Reuse the exact same prompt-polish path as /images/rewrite so
            # avatar generation does not grow a second style/prompt compiler.
            _current, polished_prompt, reference, aspect_ratio = compile_instruction(
                character_id,
                rewrite_req,
                provider=provider,
            )
            candidates = []
            for index in range(req.count):
                variation = _AVATAR_VARIATIONS[index % len(_AVATAR_VARIATIONS)]
                candidate_prompt = (
                    f"{polished_prompt}\n\n"
                    f"Candidate variation {index + 1}: {variation} "
                    "Do not change the character identity or the selected visual style."
                )
                result = provider.generate(
                    ImageGenerationRequest(
                        prompt=candidate_prompt,
                        aspect_ratio=aspect_ratio,
                        size="1K",
                        reference_images=[reference] if reference else [],
                    )
                )
                payload, _mime_type, extension = generated_payload_bytes(result.payload)
                asset = access.media_storage.save_bytes(
                    character_id=character_id,
                    original_name=(
                        f"generated-avatar-{provider_name}-{index + 1}.{extension}"
                    ),
                    payload=payload,
                    created_at=datetime.now().astimezone(),
                    source="GENERATED_AVATAR_CANDIDATE",
                )
                access.store().add_media_asset(asset)
                candidates.append(
                    {
                        "index": index + 1,
                        "media_id": asset.id,
                        "url": f"/v1/media/{asset.id}",
                        "source": asset.source,
                        "provider": result.provider,
                        "model": result.model,
                        "supports_reference_images": bool(
                            provider.supports_reference_images
                        ),
                    }
                )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "visual.avatar generate_failed character=%s provider=%s style=%s count=%s error=%s",
                character_id,
                provider_name,
                req.style,
                req.count,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail=f"头像候选生成失败：{exc}",
            ) from exc

        elapsed = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "visual.avatar generated character=%s provider=%s style=%s candidates=%d duration_ms=%.1f",
            character_id,
            provider_name,
            req.style,
            len(candidates),
            elapsed,
        )
        return {
            "character_id": character_id,
            # Compatibility for older clients that only know the singular field.
            "candidate": candidates[0],
            "candidates": candidates,
            "style": req.style,
            "style_guidance": style_guidance,
            "visual_intent": base_intent,
            "polished_prompt": polished_prompt,
            "aspect_ratio": aspect_ratio,
            "used_avatar_reference": bool(reference),
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
                    source="CHAT_ASSET" if not asset.source.startswith(("GENERATED", "DEV_GENERATED", "TOOL_GENERATED")) else "GENERATED",
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
