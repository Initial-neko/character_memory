from __future__ import annotations

import base64
import logging
import threading
from typing import Callable

from character_memory.domain.models import ActionDecision, ActionType, Event, EventType
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)


logger = logging.getLogger("character_memory.visual_runtime")
_service = None


class DirectVisualRuntime:
    """Execute direct-chat generated-image intents without blocking the chat turn.

    PersonRuntime records the GENERATE_IMAGE intent inside the normal reaction,
    then calls ``submit``. The provider/planner work happens on a daemon worker;
    successful output is persisted as a normal IMAGE chat event and is pushed to
    the existing direct SSE channel when available.
    """

    def __init__(self, access):
        self.access = access
        self.settings = access.settings

    def _provider(self):
        name = str(getattr(self.settings, "image_generation_provider", "agnes") or "agnes").strip().lower()
        services = getattr(self.access, "services", None)
        providers = getattr(services, "image_generation_providers", {}) if services is not None else {}
        # Legacy test adapters may still inject the provider map directly.
        if not providers:
            providers = getattr(self.access, "image_generation_providers", {}) or {}
        provider = providers.get(name)
        if provider is None or not provider.available():
            return name, None
        return name, provider

    def available(self) -> bool:
        _, provider = self._provider()
        return provider is not None

    def _avatar_reference(self, character_id: str) -> str | None:
        services = getattr(self.access, "services", None)
        avatar_store = getattr(services, "avatar_store", None) if services is not None else None
        # Keep standalone/runtime tests compatible while production ownership is
        # explicit in RuntimeServices.
        if avatar_store is None:
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
        # P0.19 only exposes generated visuals to direct user turns. Wake and
        # proactive paths stay cost-free until they get explicit quotas/cooldowns.
        if source_event.event_type != EventType.USER_MESSAGE:
            logger.info(
                "visual.runtime skipped_non_user_turn character=%s event_type=%s",
                source_event.character_id,
                source_event.event_type.value,
            )
            return None

        provider_name, provider = self._provider()
        if provider is None:
            logger.warning("visual.runtime skipped_unavailable character=%s provider=%s", source_event.character_id, provider_name)
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
        prompt = VisualPromptPlanner(runtime.model).compile_prompt(
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
                prompt=prompt,
                aspect_ratio=visual_aspect_ratio(purpose),
                size="1K",
                reference_images=[reference] if reference else [],
            )
        )

        # Provider work can be slow. If a newer user fact arrived meanwhile,
        # discard this stale image instead of making it appear after the new turn.
        if still_current is not None and not still_current():
            logger.info(
                "visual.runtime superseded character=%s source_event=%s provider=%s",
                source_event.character_id,
                source_event.id,
                result.provider,
            )
            return None

        label = "自拍" if purpose == VisualPurpose.SELFIE else "配图"
        suffix = result.mime_type.split("/", 1)[-1] or "png"
        asset = self.access.media_storage.save_bytes(
            character_id=source_event.character_id,
            original_name=f"generated-{purpose.value.lower()}.{suffix}",
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
                        "image_label": label,
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

    def _publish_event(self, event: Event) -> None:
        hub = getattr(self.access, "stream_hub", None)
        if hub is None:
            return
        conversation_id = str(event.metadata.get("conversation_id") or f"{event.character_id}:default")
        try:
            from character_memory.application.async_conversation import direct_channel

            hub.publish(
                direct_channel(event.character_id, conversation_id),
                "character_event",
                {
                    "id": event.id,
                    "character_id": event.character_id,
                    "event_type": event.event_type.value,
                    "event_time": event.event_time.isoformat(),
                    "content": event.content,
                    "metadata": event.metadata,
                },
            )
        except Exception:
            logger.exception("visual.runtime publish_failed event_id=%s", event.id)

    def submit(
        self,
        runtime,
        source_event: Event,
        action: ActionDecision,
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> threading.Thread | None:
        if action.type != ActionType.GENERATE_IMAGE:
            return None

        def worker() -> None:
            try:
                event = self.generate(runtime, source_event, action, still_current=still_current)
                if event is not None:
                    self._publish_event(event)
            except Exception as exc:
                # Text/state actions have already committed. Provider outages are
                # isolated from the normal chat response and only logged here.
                logger.exception(
                    "visual.runtime async_failed character=%s source_event=%s error=%s",
                    source_event.character_id,
                    source_event.id,
                    exc,
                )

        thread = threading.Thread(
            target=worker,
            name=f"visual-{source_event.character_id}-{source_event.id or 'pending'}",
            daemon=True,
        )
        thread.start()
        return thread


def configure_direct_visual_runtime(access) -> DirectVisualRuntime:
    global _service
    _service = DirectVisualRuntime(access)
    access.visual_runtime = _service
    return _service


def direct_visual_available() -> bool:
    return bool(_service is not None and _service.available())


def generate_direct_visual_action(runtime, source_event, action, *, still_current=None):
    if _service is None:
        return None
    return _service.submit(runtime, source_event, action, still_current=still_current)
