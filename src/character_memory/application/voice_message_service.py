"""State transitions for an already-persisted VOICE_MESSAGE.

The scheduler now emits VOICE_MESSAGE in both Direct and Group chat, the
materializer synthesizes through the formal TTS route, and the browser merges
pending/ready/failed updates by the original event id. This module owns only
the durable metadata transition and SSE re-publication; synthesis and audio
storage remain in voice_message_materializer.py.
"""

from __future__ import annotations

import logging

from character_memory.application.async_conversation import direct_channel
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
)

logger = logging.getLogger("character_memory.application.voice_message")


def _publish(hub, event, character_id: str, conversation_id: str) -> None:
    if hub is None:
        return
    try:
        hub.publish(
            direct_channel(character_id, conversation_id),
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
        logger.exception("voice_message.publish_failed event_id=%s", event.id)


def _apply(store, hub, event_id: int, *, character_id: str, conversation_id: str, patch: dict) -> bool:
    event = store.get_event(event_id)
    if event is None:
        logger.warning("voice_message.missing_event event_id=%s", event_id)
        return False

    # store.get_event reads the direct-chat `events` table only. Group events
    # live in `conversation_events`, a separate AUTOINCREMENT table whose ids
    # also start at 1, so the two sequences always overlap: a group event id
    # addresses an unrelated direct message here. Without this check the call
    # returns True having rewritten that stranger's row with this audio and
    # broadcast it on the direct channel, while the group row stays pending
    # forever -- a silent wrong-row write. Every direct event carries the
    # conversation_id it belongs to, so a legitimate call still passes.
    if event.character_id != character_id or event.metadata.get("conversation_id") != conversation_id:
        logger.warning("voice_message.provenance_mismatch event_id=%s", event_id)
        return False

    metadata = dict(event.metadata)
    metadata.update(patch)
    if not store.update_event_metadata(event_id, metadata):
        return False

    _publish(hub, event.model_copy(update={"metadata": metadata}), character_id, conversation_id)
    return True


def mark_voice_message_ready(
    store,
    hub,
    event_id: int,
    *,
    character_id: str,
    conversation_id: str,
    media_id: str,
    duration_ms: int | None,
) -> bool:
    return _apply(
        store,
        hub,
        event_id,
        character_id=character_id,
        conversation_id=conversation_id,
        patch={
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: media_id,
            VOICE_DURATION_MS: duration_ms,
            VOICE_ERROR: None,
        },
    )


def mark_voice_message_failed(
    store,
    hub,
    event_id: int,
    *,
    character_id: str,
    conversation_id: str,
    error: str,
) -> bool:
    return _apply(
        store,
        hub,
        event_id,
        character_id=character_id,
        conversation_id=conversation_id,
        patch={
            VOICE_STATUS: "failed",
            VOICE_MEDIA_ID: None,
            VOICE_DURATION_MS: None,
            VOICE_ERROR: str(error)[:400],
        },
    )
