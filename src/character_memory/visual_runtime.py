from __future__ import annotations

import base64
import logging
import json
import threading
from datetime import datetime, timezone
from uuid import uuid4
from typing import Callable

from character_memory.config import runtime_setting
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType
from character_memory.visual_policy import generation_event_denial
from character_memory.image_generation_store import ImageGenerationJobs
from character_memory.visual_generation import (
    ImageGenerationRequest,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)


logger = logging.getLogger("character_memory.visual_runtime")
_service = None
_BOOT_ID = uuid4().hex


class DirectVisualRuntime:
    """Execute direct-chat generated-image intents without blocking the chat turn.

    PersonRuntime records the GENERATE_IMAGE intent inside the normal reaction,
    then calls ``submit``. The provider/planner work happens on a daemon worker;
    successful output is persisted as a normal IMAGE chat event and is pushed to
    the existing direct SSE channel when available.
    """

    def __init__(self, access, *, clock=None, boot_id=None, settings=None):
        self.access = access
        self.settings = settings or access.settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.boot_id = boot_id or _BOOT_ID
        self._jobs = None
        self._jobs_lock = threading.Lock()

    def _ledger(self, store=None):
        with self._jobs_lock:
            if self._jobs is None:
                self._jobs = ImageGenerationJobs(store or self.access.store(), boot_id=self.boot_id)
        return self._jobs

    def eligibility(self, event, *, store=None):
        denial = generation_event_denial(event, autonomous_enabled=bool(getattr(self.settings, "direct_autonomous_image_generation_enabled", False)))
        if denial:
            return denial
        if not self.available():
            return "UNAVAILABLE"
        if event.event_type == EventType.USER_MESSAGE:
            return ""
        return self._ledger(store).check(event.character_id, now=self.clock(),
            daily_max=int(getattr(self.settings, "direct_autonomous_image_daily_max_generations", 3)),
            cooldown_hours=float(getattr(self.settings, "direct_autonomous_image_cooldown_hours", 8)))

    def _provider(self):
        name = str(runtime_setting(self.settings, "image_generation_provider", "agnes") or "agnes").strip().lower()
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
        self, runtime, source_event, action, *, still_current=None, action_index=0,
    ):
        denial = self.eligibility(source_event, store=runtime.store)
        if denial or action.type != ActionType.GENERATE_IMAGE:
            logger.info("visual.runtime denied source=%s reason=%s", source_event.id, denial)
            return None
        job_id = None
        if source_event.event_type != EventType.USER_MESSAGE:
            persisted = runtime.store.get_event(source_event.id) if source_event.id else None
            if persisted is None or persisted.character_id != source_event.character_id or generation_event_denial(persisted, autonomous_enabled=True):
                return None
            source_event = persisted
            if still_current is not None and not still_current():
                return None
            job_id, denial = self._ledger(runtime.store).reserve(
                character_id=source_event.character_id, source_event_id=int(source_event.id), action_index=action_index,
                request=json.dumps({"purpose": action.image_purpose, "intent": action.visual_intent}, ensure_ascii=False, sort_keys=True),
                now=self.clock(), daily_max=int(getattr(self.settings, "direct_autonomous_image_daily_max_generations", 3)),
                cooldown_hours=float(getattr(self.settings, "direct_autonomous_image_cooldown_hours", 8)),
            )
            if job_id is None:
                logger.info("visual.runtime reservation_denied source=%s reason=%s", source_event.id, denial)
                return None
        try:
            result = self._generate(runtime, source_event, action, still_current=still_current, job_id=job_id)
            if job_id is not None and result is None:
                self._jobs.finish(job_id, status="SUPERSEDED")
            return result
        except Exception as error:
            if job_id is not None:
                self._jobs.finish(job_id, status="FAILED", error=str(error))
            raise

    def _generate(
        self,
        runtime,
        source_event: Event,
        action: ActionDecision,
        *,
        still_current: Callable[[], bool] | None = None,
        job_id=None,
    ) -> Event | None:
        if action.type != ActionType.GENERATE_IMAGE:
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
            with runtime.store.transaction():
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
                        "content_scope": "SYNTHETIC_CHARACTER_EXPRESSION",
                        "generation_job_id": job_id,
                        "generation_purpose": purpose.value,
                        "provider": result.provider,
                        "model": result.model,
                        "source_event_id": source_event.id,
                        "source_event_type": source_event.event_type.value,
                        "conversation_id": str(source_event.metadata.get("conversation_id") or f"{source_event.character_id}:default"),
                    },
                )
                )
                if job_id is not None and not self._jobs.finish(job_id, status="SUCCEEDED", event_id=event.id):
                    raise RuntimeError("image reservation no longer owned by this worker")
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
        action_index: int = 0,
    ) -> threading.Thread | None:
        if action.type != ActionType.GENERATE_IMAGE:
            return None

        def worker() -> None:
            try:
                event = self.generate(runtime, source_event, action, still_current=still_current, action_index=action_index)
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


def direct_visual_permission(event, *, store=None):
    denial = generation_event_denial(event)
    if _service is None:
        return denial or "UNAVAILABLE"
    return _service.eligibility(event, store=store)


def generate_direct_visual_action(runtime, source_event, action, *, still_current=None, action_index=0):
    if _service is None:
        return None
    return _service.submit(runtime, source_event, action, still_current=still_current, action_index=action_index)
