from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from character_memory.domain.models import ActionDecision, ActionType, EXPRESSIVE_ACTIONS
from character_memory.voice_message_fields import voice_pending_fields


@dataclass(frozen=True)
class MaterializedMessage:
    """Channel-neutral visible message produced from one expressive action."""

    content: str
    metadata: dict[str, Any]


def materialize_expressive_action(
    action: ActionDecision,
    *,
    action_index: int,
    sticker_catalog=None,
    image_catalog=None,
    base_metadata: dict[str, Any] | None = None,
) -> MaterializedMessage | None:
    """Convert one Person action into canonical message content/metadata.

    Direct and Group persistence intentionally use different tables, but the
    outward action contract must not be implemented twice. Channel-specific
    provenance belongs in base_metadata; resource and voice fields are canonical
    here.
    """

    if action.type not in EXPRESSIVE_ACTIONS:
        return None

    metadata: dict[str, Any] = {
        **(base_metadata or {}),
        "action": action.type.value,
        "action_index": int(action_index),
    }

    if action.type == ActionType.STICKER:
        sticker = sticker_catalog.get(action.sticker_id) if sticker_catalog is not None else None
        if sticker is None:
            return None
        metadata.update(
            {
                "sticker_id": sticker.id,
                "sticker_label": sticker.label,
            }
        )
        meaning = "、".join(getattr(sticker, "tags", []) or []) or str(getattr(sticker, "description", "") or "")
        if meaning:
            metadata["sticker_meaning"] = meaning
        return MaterializedMessage(content=f"[表情包：{sticker.label}]", metadata=metadata)

    if action.type == ActionType.IMAGE:
        image = image_catalog.get(action.image_id) if image_catalog is not None else None
        if image is None:
            return None
        metadata.update({"image_id": image.id, "image_label": image.label})
        return MaterializedMessage(content=f"[图片：{image.label}]", metadata=metadata)

    message = (action.message or "").strip()
    if not message:
        return None
    if action.type == ActionType.VOICE_MESSAGE:
        metadata.update(voice_pending_fields())
    return MaterializedMessage(content=message, metadata=metadata)
