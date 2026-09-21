from __future__ import annotations

from character_memory.domain.models import EventType
from character_memory.images import load_image_catalog
from character_memory.storage.chat_history import ChatHistoryRepository
from character_memory.voice_message_fields import voice_fields


def attach_history_routes(app):
    """Attach read-only paged chat history routes.

    This stays outside the runtime write API so paging work cannot accidentally
    alter the chat transaction path.
    """
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before history routes are attached")

    def profiles_by_id():
        return {item["id"]: item for item in access.character_profiles()}

    def ensure_character(character_id: str):
        profile = profiles_by_id().get(character_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")
        return profile

    def sticker_payload(catalog, sticker_id: str | None):
        if not sticker_id:
            return None
        sticker = catalog.get(sticker_id)
        if sticker is None or catalog.asset_path(sticker_id) is None:
            return None
        return {**sticker.model_dump(mode="json"), "url": f"/v1/stickers/{sticker.id}/asset"}

    def image_payload(character_id: str, catalog, image_id: str | None):
        if not image_id:
            return None
        image = catalog.get(image_id)
        if image is None or catalog.asset_path(image_id) is None:
            return None
        return {
            **image.model_dump(mode="json"),
            "source": "CHARACTER_LIBRARY",
            "url": f"/v1/images/{character_id}/{image.id}/asset",
        }

    def media_payload(media_id: str | None):
        if not media_id:
            return None
        asset = access.store().get_media_asset(media_id)
        if asset is None or access.media_storage.asset_path(asset) is None:
            return None
        return {
            "id": asset.id,
            "label": asset.original_name,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "source": asset.source,
            "url": f"/v1/media/{asset.id}",
        }

    def message_payload(event, has_trace: bool, sticker_catalog, image_catalog):
        if event.event_type == EventType.USER_MESSAGE:
            role = "user"
            source_event_id = event.id
            source_event_type = EventType.USER_MESSAGE.value
            content = event.metadata.get("display_text", event.content)
        else:
            role = "assistant"
            source_event_id = event.metadata.get("source_event_id")
            source_event_type = event.metadata.get("source_event_type")
            content = event.content

        sticker_id = event.metadata.get("sticker_id")
        sticker = sticker_payload(sticker_catalog, sticker_id)
        image = image_payload(event.character_id, image_catalog, event.metadata.get("image_id"))
        media_id = event.metadata.get("media_id")
        if media_id:
            image = media_payload(media_id)

        preview = content
        if sticker is not None and (not str(content or "").strip() or event.metadata.get("action") == "STICKER"):
            preview = f"[表情包] {sticker['label']}"
        if image is not None and (not str(content or "").strip() or event.metadata.get("action") == "IMAGE" or media_id):
            prefix = str(content or "").strip()
            media_preview = f"[图片] {image['label']}"
            preview = f"{prefix} {media_preview}".strip() if prefix else media_preview

        return {
            "id": event.id,
            "role": role,
            "content": content,
            "preview": preview,
            "event_time": event.event_time.isoformat(),
            "action": event.metadata.get("action"),
            "action_index": event.metadata.get("action_index"),
            "sticker_id": sticker_id,
            "sticker": sticker,
            "image_id": event.metadata.get("image_id"),
            "media_id": media_id,
            "image": image,
            # Deliberately a distinct key from "media_id" above, which drives
            # image rendering.
            **voice_fields(event.metadata),
            "source_event_type": source_event_type,
            "source_event_id": source_event_id,
            "proactive": source_event_type == EventType.PROACTIVE_INTENT.value,
            "has_trace": has_trace,
        }

    @app.get("/v1/chat/history-page")
    def direct_history_page(character_id: str = "rin", limit: int = 50, before_id: int | None = None):
        profile = ensure_character(character_id)
        repository = ChatHistoryRepository(access.store())
        page = repository.list_page(character_id, limit=limit, before_id=before_id)
        source_ids = []
        for event in page.events:
            if event.event_type == EventType.USER_MESSAGE:
                source_ids.append(event.id)
            else:
                source_ids.append(event.metadata.get("source_event_id"))
        trace_sources = repository.trace_sources(character_id, source_ids)

        # One resource snapshot per page. Do not reparse manifests for every row.
        sticker_catalog = access.global_sticker_catalog()
        image_catalog = load_image_catalog(profile["persona_path"])
        return {
            "character_id": character_id,
            "messages": [
                message_payload(
                    event,
                    (event.id if event.event_type == EventType.USER_MESSAGE else event.metadata.get("source_event_id")) in trace_sources,
                    sticker_catalog,
                    image_catalog,
                )
                for event in page.events
            ],
            "has_more": page.has_more,
            "next_before_id": page.next_before_id,
        }

    return app
