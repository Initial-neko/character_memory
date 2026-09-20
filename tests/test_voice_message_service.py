from datetime import datetime, timezone

from character_memory.application.voice_message_service import (
    mark_voice_message_failed,
    mark_voice_message_ready,
)
from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
    voice_pending_fields,
)


class _FakeHub:
    def __init__(self):
        self.published = []

    def publish(self, channel_key, event_type, data):
        self.published.append((channel_key, event_type, data))
        return len(self.published)


def _pending(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", **voice_pending_fields()},
        )
    )
    return store, saved


def test_mark_ready_sets_the_asset_and_republishes_the_same_id(tmp_path):
    store, saved = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_ready(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            media_id="abc123",
            duration_ms=1840,
        )
        is True
    )

    metadata = store.get_event(saved.id).metadata
    assert metadata[VOICE_STATUS] == "ready"
    assert metadata[VOICE_MEDIA_ID] == "abc123"
    assert metadata[VOICE_DURATION_MS] == 1840
    # update_event_metadata REPLACES the document, so the patch must be merged
    # into the existing metadata rather than written over it: metadata_json has
    # no schema validation, and a wholesale replace would drop this key -- and
    # every other key the patch does not name -- with no error anywhere.
    assert metadata["action"] == "VOICE_MESSAGE"

    channel_key, event_type, data = hub.published[0]
    assert event_type == "character_event"
    # The same id is what makes the client merge update in place.
    assert data["id"] == saved.id
    assert data["metadata"][VOICE_STATUS] == "ready"
    assert channel_key.endswith("momo:default")


def test_mark_failed_records_the_reason_and_keeps_the_text(tmp_path):
    store, saved = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_failed(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            error="provider unavailable",
        )
        is True
    )

    reloaded = store.get_event(saved.id)
    assert reloaded.content == "晚上好呀"
    assert reloaded.metadata[VOICE_STATUS] == "failed"
    assert reloaded.metadata[VOICE_ERROR] == "provider unavailable"
    assert reloaded.metadata[VOICE_MEDIA_ID] is None


def test_mark_failed_after_ready_clears_the_stale_media_reference(tmp_path):
    """A message that had audio and then failed must not keep pointing at it.

    The pending -> failed path cannot observe this: the pending fixture already
    seeds both keys as None, so a failed patch that omits the explicit clear
    would look identical. Only ready -> failed exercises the clear.
    """
    store, saved = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_ready(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            media_id="abc123",
            duration_ms=1840,
        )
        is True
    )
    assert store.get_event(saved.id).metadata[VOICE_MEDIA_ID] == "abc123"

    assert (
        mark_voice_message_failed(
            store,
            hub,
            saved.id,
            character_id="momo",
            conversation_id="momo:default",
            error="provider unavailable",
        )
        is True
    )

    metadata = store.get_event(saved.id).metadata
    assert metadata[VOICE_STATUS] == "failed"
    assert metadata[VOICE_MEDIA_ID] is None
    assert metadata[VOICE_DURATION_MS] is None
    assert metadata[VOICE_ERROR] == "provider unavailable"


def test_unknown_event_id_is_reported_not_raised(tmp_path):
    store, _ = _pending(tmp_path)
    hub = _FakeHub()

    assert (
        mark_voice_message_ready(
            store,
            hub,
            999999,
            character_id="momo",
            conversation_id="momo:default",
            media_id="abc123",
            duration_ms=None,
        )
        is False
    )
    assert hub.published == []
