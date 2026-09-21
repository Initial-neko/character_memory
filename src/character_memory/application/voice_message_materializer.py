"""Materialize persisted VOICE_MESSAGE events through the formal TTS route."""

from __future__ import annotations

import logging

import httpx

from character_memory.application.async_conversation import direct_channel, group_channel
from character_memory.application.voice_message_service import mark_voice_message_failed, mark_voice_message_ready
from character_memory.group_store import GroupRepository
from character_memory.voice_message_fields import VOICE_DURATION_MS, VOICE_ERROR, VOICE_MEDIA_ID, VOICE_STATUS

logger = logging.getLogger("character_memory.application.voice_message_materializer")


class VoiceMessageMaterializer:
    """Turn one durable pending voice event into one durable audio asset.

    One VOICE_MESSAGE always produces exactly one /v1/tts request containing the
    complete message text. The event already exists before this class runs, so
    provider failure degrades the bubble instead of deleting the message.
    """

    def __init__(self, store_provider, media_storage, hub, *, media_base: str, client=None):
        self.store_provider = store_provider
        self.media_storage = media_storage
        self.hub = hub
        self.media_base = media_base.rstrip("/")
        self.client = client or httpx.Client(timeout=180.0)

    @staticmethod
    def _duration_ms(response) -> int | None:
        raw = response.headers.get("x-media-audio-ms")
        try:
            value = int(float(raw))
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    def _synthesize(self, *, character_id: str, text: str):
        response = self.client.post(
            f"{self.media_base}/v1/tts",
            json={"text": text, "voice": character_id},
        )
        response.raise_for_status()
        return response

    def _save(self, *, store, character_id: str, event_id: int, event_time, response):
        asset = self.media_storage.save_bytes(
            character_id=character_id,
            original_name=f"voice-message-{event_id}",
            payload=response.content,
            created_at=event_time,
            source="VOICE_MESSAGE",
        )
        store.add_media_asset(asset)
        return asset

    def materialize_direct(self, event, *, conversation_id: str) -> None:
        store = self.store_provider()
        try:
            response = self._synthesize(character_id=event.character_id, text=event.content)
            asset = self._save(
                store=store,
                character_id=event.character_id,
                event_id=int(event.id),
                event_time=event.event_time,
                response=response,
            )
            mark_voice_message_ready(
                store,
                self.hub,
                int(event.id),
                character_id=event.character_id,
                conversation_id=conversation_id,
                media_id=asset.id,
                duration_ms=self._duration_ms(response),
            )
        except Exception as exc:
            logger.exception("voice_message.materialize_direct_failed event_id=%s", event.id)
            mark_voice_message_failed(
                store,
                self.hub,
                int(event.id),
                character_id=event.character_id,
                conversation_id=conversation_id,
                error=str(exc),
            )

    def materialize_group(self, raw_event: dict) -> None:
        store = self.store_provider()
        repo = GroupRepository(store)
        metadata = dict(raw_event.get("metadata") or {})
        event_id = int(raw_event["id"])
        conversation_id = str(raw_event["conversation_id"])
        character_id = str(raw_event["actor_id"])
        try:
            response = self._synthesize(character_id=character_id, text=str(raw_event.get("content") or ""))
            from datetime import datetime
            event_time = datetime.fromisoformat(str(raw_event["event_time"]))
            asset = self._save(
                store=store,
                character_id=character_id,
                event_id=event_id,
                event_time=event_time,
                response=response,
            )
            metadata.update({
                VOICE_STATUS: "ready",
                VOICE_MEDIA_ID: asset.id,
                VOICE_DURATION_MS: self._duration_ms(response),
                VOICE_ERROR: None,
            })
        except Exception as exc:
            logger.exception("voice_message.materialize_group_failed event_id=%s", event_id)
            metadata.update({
                VOICE_STATUS: "failed",
                VOICE_MEDIA_ID: None,
                VOICE_DURATION_MS: None,
                VOICE_ERROR: str(exc)[:400],
            })
        if not repo.update_event_metadata(event_id, metadata):
            return
        updated = {**raw_event, "metadata": metadata}
        self.hub.publish(group_channel(conversation_id), "group_character_event", updated)
