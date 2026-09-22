from __future__ import annotations

from typing import Any

from character_memory.domain.models import EventType
from character_memory.voice_message_fields import voice_fields


def upload_caption(original_name: str | None) -> str | None:
    """The caption a chat bubble may print for an uploaded or generated image.

    ``original_name`` is the name a file arrived under, never a description of
    what is in it: the file picker's ``photo.png``, a capture's
    ``visual-camera-1758...jpg``, a generated image's uuid, the voice path's
    ``voice-message-12``. Four payload builders handed that name to the bubble
    as ``label`` and the bubble printed it under the picture, so a user's own
    photo arrived captioned with its own filename and a generated one with a
    uuid. No caption says less than a filename, and says it truthfully; this is
    the one place that changes when an asset grows a real description.

    Character-library images and stickers keep their labels -- those are
    written for a reader, and are not routed through here.
    """

    return None


def common_chat_message_fields(
    metadata: dict[str, Any],
    *,
    sticker: dict | None = None,
    image: dict | None = None,
) -> dict[str, Any]:
    """Canonical resource/voice fields shared by every chat projection."""

    return {
        "action": metadata.get("action"),
        "action_index": metadata.get("action_index"),
        "sticker_id": metadata.get("sticker_id"),
        "sticker": sticker,
        "image_id": metadata.get("image_id"),
        "media_id": metadata.get("media_id"),
        "image": image,
        **voice_fields(metadata),
    }


def project_direct_message(
    event,
    *,
    sticker: dict | None = None,
    image: dict | None = None,
    has_trace: bool | None = None,
    include_legacy_labels: bool = False,
) -> dict[str, Any]:
    """Project one durable Direct Event into the canonical browser/API message."""

    is_user = event.event_type == EventType.USER_MESSAGE
    role = "user" if is_user else "assistant"
    source_event_id = event.id if is_user else event.metadata.get("source_event_id")
    source_event_type = (
        EventType.USER_MESSAGE.value
        if is_user
        else event.metadata.get("source_event_type")
    )
    content = event.metadata.get("display_text", event.content) if is_user else event.content
    media_id = event.metadata.get("media_id")

    preview = content
    if sticker is not None and (
        not str(content or "").strip() or event.metadata.get("action") == "STICKER"
    ):
        preview = f"[表情包] {sticker['label']}"
    if image is not None and (
        not str(content or "").strip()
        or event.metadata.get("action") == "IMAGE"
        or media_id
    ):
        prefix = str(content or "").strip()
        caption = str(image.get("label") or "").strip()
        media_preview = f"[图片] {caption}".strip() if caption else "[图片]"
        preview = f"{prefix} {media_preview}".strip() if prefix else media_preview

    payload: dict[str, Any] = {
        "id": event.id,
        "role": role,
        "content": content,
        "preview": preview,
        "event_time": event.event_time.isoformat(),
        **common_chat_message_fields(
            event.metadata,
            sticker=sticker,
            image=image,
        ),
        "source_event_type": source_event_type,
        "source_event_id": source_event_id,
        "proactive": source_event_type == EventType.PROACTIVE_INTENT.value,
    }
    if has_trace is not None:
        payload["has_trace"] = bool(has_trace)
    if include_legacy_labels:
        payload.update(
            {
                "sticker_label": event.metadata.get("sticker_label"),
                "image_label": event.metadata.get("image_label"),
                "media_name": event.metadata.get("media_name"),
            }
        )
    return payload


def project_group_message(
    event,
    *,
    actor_name: str,
    sticker: dict | None = None,
    image: dict | None = None,
    turn_summary: dict | None = None,
) -> dict[str, Any]:
    """Project one shared Group Event without changing Group-specific identity."""

    role = "user" if event.actor_type == "USER" else "assistant"
    content = (
        event.metadata.get("display_text", event.content)
        if role == "user"
        else event.content
    )
    return {
        "id": event.id,
        "conversation_id": event.conversation_id,
        "turn_id": event.turn_id,
        "role": role,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "actor_name": actor_name,
        "content": content,
        "event_time": event.event_time.isoformat(),
        **common_chat_message_fields(
            event.metadata,
            sticker=sticker,
            image=image,
        ),
        "mentions": event.metadata.get("mentions", []) if role == "user" else [],
        "source_conversation_event_id": event.metadata.get(
            "source_conversation_event_id"
        ),
        "turn_summary": turn_summary if role == "user" else None,
    }
