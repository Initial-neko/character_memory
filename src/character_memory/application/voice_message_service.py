"""State transitions for a persisted VOICE_MESSAGE.

Synthesis is not performed here. The caller synthesizes, stores the audio as a
media asset, then reports the outcome through these two functions. Each one
updates the event's metadata and re-publishes the event under its ORIGINAL id,
so a client that merges an incoming event into its history by id can update the
existing bubble in place instead of appending a second one.

That is all the id buys: the client side of the contract is not converged yet.
The browser builds a message through CM.directEventToMessage, which reads
sticker_id / image_id / action / source_event_* and none of the voice keys, so
the voice state never reaches the client's message state and no bubble flips
out of pending today. Wiring those keys through is deferred to the synthesis
work; nothing depends on it yet, because no runtime path emits VOICE_MESSAGE.
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
