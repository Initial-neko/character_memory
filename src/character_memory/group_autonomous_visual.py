from __future__ import annotations

from datetime import datetime
import logging
import threading
from typing import Any

from character_memory.application import group_conversation_service as group_service_module
from character_memory.application.async_conversation import group_channel
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import ActionDecision, ActionType
from character_memory.group_store import GroupEvent, GroupRepository
from character_memory.images import load_image_catalog
from character_memory.visual_generation import ImageGenerationRequest, VisualPromptPlanner, VisualPurpose, visual_aspect_ratio
from character_memory.visual_runtime import direct_visual_available


logger = logging.getLogger("character_memory.group_autonomous_visual")
_access = None
_installed = False
_original_group_compile_context = group_service_module.compile_context
_original_react_member = GroupConversationService._react_member


def _latest_group_user_id(service: GroupConversationService, conversation_id: str) -> int | None:
    with service.store._lock:
        row = service.store.conn.execute(
            "SELECT id FROM conversation_events WHERE conversation_id=? AND actor_type='USER' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    return int(row["id"]) if row else None


def _profile_name(character_id: str) -> str:
    if _access is None:
        return character_id
    for profile in _access.character_profiles():
        if profile.get("id") == character_id:
            return str(profile.get("name") or character_id)
    return character_id


def _group_recent_dialogue(store, conversation_id: str) -> list[str]:
    events = GroupRepository(store).list_events(conversation_id, limit=10)[-8:]
    lines: list[str] = []
    for item in events:
        if item.actor_type == "USER":
            role = "用户"
            content = item.metadata.get("display_text", item.content)
        else:
            role = _profile_name(item.actor_id)
            if item.metadata.get("action") == ActionType.STICKER.value:
                content = f"[表情包：{item.metadata.get('sticker_label') or '表情包'}]"
            elif item.metadata.get("action") == ActionType.IMAGE.value:
                content = f"[图片：{item.metadata.get('image_label') or '图片'}]"
            else:
                content = item.content
        text = " ".join(str(content or "").split()).strip()
        if text:
            lines.append(f"{role}: {text[:360]}")
    return lines


def _visual_runtime():
    return getattr(_access, "visual_runtime", None) if _access is not None else None


def _generate_group_image(
    service: GroupConversationService,
    *,
    group,
    source_event: GroupEvent,
    character_id: str,
    action: ActionDecision,
) -> GroupEvent | None:
    visual_runtime = _visual_runtime()
    if visual_runtime is None or not direct_visual_available():
        return None

    provider_name, provider = visual_runtime._provider()
    if provider is None:
        logger.warning("group.visual skipped_unavailable character=%s provider=%s", character_id, provider_name)
        return None

    try:
        purpose = VisualPurpose(str(action.image_purpose or "").upper())
    except ValueError:
        logger.warning("group.visual skipped_bad_purpose character=%s purpose=%s", character_id, action.image_purpose)
        return None
    if purpose not in {VisualPurpose.SELFIE, VisualPurpose.SCENE}:
        return None

    reference = None
    if purpose == VisualPurpose.SELFIE and provider.supports_reference_images:
        reference = visual_runtime._avatar_reference(character_id)

    runtime = service.runtimes[character_id]
    state = service.store.get_mental_state(character_id, at=source_event.event_time)
    prompt = VisualPromptPlanner(runtime.model).compile_prompt(
        character_id,
        purpose=purpose,
        persona=runtime.persona,
        mental_state=state,
        recent_dialogue=_group_recent_dialogue(service.store, group.id),
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

    # Match direct-chat stale-result semantics: if the room has moved on to a
    # newer user fact while image generation was running, do not inject an old
    # result into the newer turn.
    if _latest_group_user_id(service, group.id) != int(source_event.id):
        logger.info(
            "group.visual superseded conversation=%s character=%s source_event=%s provider=%s",
            group.id,
            character_id,
            source_event.id,
            result.provider,
        )
        return None

    label = "自拍" if purpose == VisualPurpose.SELFIE else "配图"
    suffix = result.mime_type.split("/", 1)[-1] or "png"
    asset = _access.media_storage.save_bytes(
        character_id=character_id,
        original_name=f"generated-{purpose.value.lower()}.{suffix}",
        payload=result.payload,
        created_at=source_event.event_time,
        source=f"GENERATED_{purpose.value}",
    )
    service.store.add_media_asset(asset)

    repository = GroupRepository(service.store)
    try:
        event = repository.append_event(
            GroupEvent(
                conversation_id=group.id,
                turn_id=source_event.turn_id,
                actor_type="CHARACTER",
                actor_id=character_id,
                event_type="CHARACTER_MESSAGE",
                event_time=source_event.event_time,
                content=f"[生成图片：{label}]",
                metadata={
                    "action": ActionType.IMAGE.value,
                    # Keep image_id for the existing realtime group renderer, and
                    # media_id for durable history where generated bytes belong.
                    "image_id": asset.id,
                    "media_id": asset.id,
                    "image_label": label,
                    "generated": True,
                    "generation_purpose": purpose.value,
                    "provider": result.provider,
                    "model": result.model,
                    "source_conversation_event_id": source_event.id,
                },
            )
        )
    except Exception:
        service.store.delete_media_asset(asset.id)
        _access.media_storage.delete(asset)
        raise

    hub = getattr(_access, "stream_hub", None)
    if hub is not None:
        hub.publish(group_channel(group.id), "group_character_event", event.model_dump(mode="json"))

    logger.info(
        "group.visual generated conversation=%s character=%s source_event=%s purpose=%s provider=%s model=%s media=%s",
        group.id,
        character_id,
        source_event.id,
        purpose.value,
        result.provider,
        result.model,
        asset.id,
    )
    return event


def _submit_group_image(service: GroupConversationService, *, group, source_event: GroupEvent, character_id: str, action: ActionDecision) -> threading.Thread:
    def worker() -> None:
        try:
            _generate_group_image(
                service,
                group=group,
                source_event=source_event,
                character_id=character_id,
                action=action,
            )
        except Exception as exc:
            # The member's text/state reaction has already committed. ImageGen
            # failure therefore stays isolated, matching direct chat semantics.
            logger.exception(
                "group.visual async_failed conversation=%s character=%s source_event=%s error=%s",
                group.id,
                character_id,
                source_event.id,
                exc,
            )

    thread = threading.Thread(
        target=worker,
        name=f"group-visual-{character_id}-{source_event.id or 'pending'}",
        daemon=True,
    )
    thread.start()
    return thread


def _group_compile_context(*args, **kwargs):
    # Group Character reactions use the same GENERATE_IMAGE contract as direct
    # USER_MESSAGE turns whenever the configured visual provider is available.
    kwargs["allow_generate_image"] = bool(direct_visual_available())
    return _original_group_compile_context(*args, **kwargs)


def _react_member_with_visual(self: GroupConversationService, *args, **kwargs) -> dict[str, Any]:
    decision = _original_react_member(self, *args, **kwargs)
    group = kwargs.get("group")
    source_event = kwargs.get("source_event")
    character_id = kwargs.get("character_id")
    if group is None or source_event is None or not character_id:
        return decision

    generation = None
    for raw in decision.get("actions") or []:
        if str(raw.get("type") or "").upper() != ActionType.GENERATE_IMAGE.value:
            continue
        try:
            generation = ActionDecision.model_validate(raw)
        except Exception:
            logger.exception("group.visual invalid_action conversation=%s character=%s raw=%s", group.id, character_id, raw)
        break
    if generation is not None:
        _submit_group_image(
            self,
            group=group,
            source_event=source_event,
            character_id=str(character_id),
            action=generation,
        )
    return decision


def _attach_generated_media_image_route(app) -> None:
    """Make generated MediaAsset IDs usable by the existing group SSE renderer.

    groups.js already renders IMAGE actions through /v1/images/{character}/{id}/asset.
    Generated images live in MediaStorage instead of Persona image catalogs, so
    this high-priority compatible route first checks MediaStorage and then falls
    back to the normal character image catalog.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get("/v1/images/{character_id}/{image_id}/asset", include_in_schema=False)
    def generated_or_library_image_asset(character_id: str, image_id: str):
        asset = _access.store().get_media_asset(image_id)
        if asset is not None and str(getattr(asset, "character_id", "")) == character_id:
            path = _access.media_storage.asset_path(asset)
            if path is not None:
                return FileResponse(path, media_type=asset.mime_type, filename=asset.original_name)

        profile = next((item for item in _access.character_profiles() if item.get("id") == character_id), None)
        if profile is None:
            raise HTTPException(status_code=404, detail="image not found")
        catalog = load_image_catalog(profile["persona_path"])
        path = catalog.asset_path(image_id)
        if path is None:
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(path)

    # FastAPI/Starlette matches routes in insertion order. create_api() already
    # owns the same public path for persona-library images, so move this compatible
    # superset route ahead of it rather than changing the browser contract.
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)


def install_group_autonomous_visual(app) -> None:
    global _access, _installed
    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before group visual install")
    _access = access

    if not _installed:
        group_service_module.compile_context = _group_compile_context
        GroupConversationService._react_member = _react_member_with_visual
        _installed = True

    _attach_generated_media_image_route(app)
    logger.info("group.visual installed")
