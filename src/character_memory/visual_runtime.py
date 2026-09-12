from __future__ import annotations

import base64
import logging
from typing import Callable

from character_memory.domain.models import ActionDecision, ActionType, Event, EventType
from character_memory.visual_generation import ImageGenerationRequest, VisualPromptPlanner, VisualPurpose


logger = logging.getLogger("character_memory.visual_runtime")
_service = None


class DirectVisualRuntime:
    """Execute character-owned generated-image intents for direct chat only.

    Group orchestration never calls this service. GENERATE_IMAGE remains an
    internal decision until this service successfully creates a durable IMAGE
    event backed by a local MediaAsset.
    """

    def __init__(self, access):
        self.access = access
        self.settings = access.settings

    def _provider(self):
        name = str(getattr(self.settings, "image_generation_provider", "agnes") or "agnes").strip().lower()
        providers = getattr(self.access, "image_generation_providers", {}) or {}
        provider = providers.get(name)
        if provider is None or not provider.available():
            return name, None
        return name, provider

    def available(self) -> bool:
        _, provider = self._provider()
        return provider is not None

    def _avatar_reference(self, character_id: str) -> str | None:
        avatar_store = getattr(self.access, "avatar_store", None)
        if avatar_store is None:
            return None
        path = avatar_store.asset_path(character_id)
        metadata = avatar_store.load(character_id)
        if path is None or metadata is None:
            return None
        return f"data:{metadata.content_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    @staticmethod
    def _recent_dialogue(runtime, character_id: str, before) -> list[str]:
        events = runtime.store.list_events(character_id, limit=10, before=before)[-8:]
        result: list[str] = []
        for item in events:
            if item.event_type == EventType.USER_MESSAGE:
                role = "用户"
                content = item.metadata.get("display_text", item.content)
            elif item.event_type == EventType.CHARACTER_MESSAGE:
                role = "角色"
                content = item.content
            else:
                continue
            text = " ".join(str(content or "").split()).strip()
            if text:
                result.append(f"{role}: {text[:360]}")
        return result

    def generate(
        self,
        runtime,
        source_event: Event,
        action: ActionDecision,
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> Event | None:
        if action.type != ActionType.GENERATE_IMAGE:
            return None
        provider_name, provider = self._provider()
        if provider is None:
            logger.warning("visual.runtime skipped_unconfigured character=%s provider=%s", source_event.character_id, provider_name)
            return None
        try:
            purpose = VisualPurpose(str(action.image_purpose or "").upper())
        except ValueError:
            logger.warning("visual.runtime skipped_bad_purpose character=%s purpose=%s", source_event.character_id, action.image_purpose)
            return None
        if purpose not in {VisualPurpose.SELFIE, VisualPurpose.SCENE}:
            return None

        reference = None
        if purpose == VisualPurpose.SELFIE and provider.supports_reference_images:
            reference = self._avatar_reference(source_event.character_id)

        state = runtime.store.get_mental_state(source_event.character_id, at=source_event.event_time)
        plan = VisualPromptPlanner(runtime.model).plan(
            source_event.character_id,
            purpose=purpose,
            persona=runtime.persona,
            mental_state=state,
            recent_dialogue=self._recent_dialogue(runtime, source_event.character_id, source_event.event_time),
            visual_intent=str(action.visual_intent or "").strip(),
            has_reference_image=bool(reference),
        )
        result = provider.generate(
            ImageGenerationRequest(
                prompt=plan.positive_prompt,
                negative_prompt=plan.negative_prompt,
                aspect_ratio=plan.aspect_ratio,
                size="1K",
                reference_images=[reference] if reference else [],
            )
        )

        # A newer user fact may arrive while a slow image provider is working.
        # Do not let a stale image appear after that newer turn.
        if still_current is not None and not still_current():
            logger.info(
                "visual.runtime superseded character=%s source_event=%s provider=%s",
                source_event.character_id,
                source_event.id,
                result.provider,
            )
            return None

        label = "自拍" if purpose == VisualPurpose.SELFIE else "配图"
        asset = self.access.media_storage.save_bytes(
            character_id=source_event.character_id,
            original_name=f"generated-{purpose.value.lower()}.{result.mime_type.split('/')[-1]}",
            payload=result.payload,
            created_at=source_event.event_time,
            source=f"GENERATED_{purpose.value}",
        )
        runtime.store.add_media_asset(asset)
        try:
            event = runtime.store.append_event(
                Event(
                    character_id=source_event.character_id,
                    event_type=EventType.CHARACTER_MESSAGE,
                    event_time=source_event.event_time,
                    content=f"[生成图片：{label}]",
                    metadata={
                        "action": ActionType.IMAGE.value,
                        "media_id": asset.id,
                        "generated": True,
                        "generation_purpose": purpose.value,
                        "provider": result.provider,
                        "model": result.model,
                        "source_event_id": source_event.id,
                        "source_event_type": source_event.event_type.value,
                        "conversation_id": str(source_event.metadata.get("conversation_id") or f"{source_event.character_id}:default"),
                    },
                )
            )
        except Exception:
            runtime.store.delete_media_asset(asset.id)
            self.access.media_storage.delete(asset)
            raise
        logger.info(
            "visual.runtime generated character=%s source_event=%s purpose=%s provider=%s model=%s media=%s",
            source_event.character_id,
            source_event.id,
            purpose.value,
            result.provider,
            result.model,
            asset.id,
        )
        return event


def configure_direct_visual_runtime(access) -> DirectVisualRuntime:
    global _service
    _service = DirectVisualRuntime(access)
    return _service


def direct_visual_available() -> bool:
    return bool(_service is not None and _service.available())


def generate_direct_visual_action(runtime, source_event, action, *, still_current=None):
    if _service is None:
        return None
    return _service.generate(runtime, source_event, action, still_current=still_current)
