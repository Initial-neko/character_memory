from __future__ import annotations

from typing import Any

from character_memory.voice_message_fields import voice_fields


def common_chat_message_fields(
    metadata: dict[str, Any],
    *,
    sticker: dict | None = None,
    image: dict | None = None,
) -> dict[str, Any]:
    """Canonical resource/voice projection shared by Direct and Group history."""

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
