from datetime import datetime, timezone

from character_memory.application.voice_message_service import (
    mark_voice_message_failed,
    mark_voice_message_ready,
)
from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent, GroupRepository
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


CONVERSATION_ID = "momo:default"


def _pending(tmp_path):
    store = SQLiteStore(tmp_path / "voice.db")
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            # conversation_id is what person_runtime writes on every direct
            # event (runtime/person_runtime.py), and it is what the service
            # checks to prove the loaded row belongs to this conversation.
            metadata={
                "action": "VOICE_MESSAGE",
                "conversation_id": CONVERSATION_ID,
                **voice_pending_fields(),
            },
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
            conversation_id=CONVERSATION_ID,
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
            conversation_id=CONVERSATION_ID,
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
            conversation_id=CONVERSATION_ID,
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
            conversation_id=CONVERSATION_ID,
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
            conversation_id=CONVERSATION_ID,
            media_id="abc123",
            duration_ms=None,
        )
        is False
    )
    assert hub.published == []


def test_a_group_event_id_never_rewrites_the_direct_message_with_that_id(tmp_path):
    """Group events live in another table with their own id sequence.

    conversation_events (group_store.py) and events (storage/sqlite.py) are
    separate AUTOINCREMENT tables whose ids both start at 1, so the two
    sequences always overlap. A group history id passed to the service used to
    address the events table blindly: it did not raise, it rewrote whichever
    unrelated direct message held that id to ready with someone else's audio,
    broadcast it on the direct channel, and returned True -- while the real
    group row stayed pending forever. That silent wrong-row write is the whole
    failure mode. The service must refuse a row that does not belong to the
    character and conversation it was called for.
    """
    store, direct = _pending(tmp_path)
    hub = _FakeHub()

    repository = GroupRepository(store)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    group = repository.create_group("测试群", ["momo"], now)
    group_event = repository.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id="momo",
            event_type="CHARACTER_MESSAGE",
            event_time=now,
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", **voice_pending_fields()},
        )
    )
    # The collision is the precondition, not an accident of the fixture: both
    # tables hand out 1 first.
    assert group_event.id == direct.id
    assert group.id != CONVERSATION_ID

    assert (
        mark_voice_message_ready(
            store,
            hub,
            group_event.id,
            character_id="momo",
            conversation_id=group.id,
            media_id="GROUP_AUDIO",
            duration_ms=1234,
        )
        is False
    )

    # The direct row is the one the bug used to corrupt; it must be untouched.
    metadata = store.get_event(direct.id).metadata
    assert metadata[VOICE_STATUS] == "pending"
    assert metadata[VOICE_MEDIA_ID] is None
    assert metadata[VOICE_DURATION_MS] is None
    # Nothing may be pushed on the direct channel for a group id either: the
    # wrong-channel broadcast told the browser a direct bubble had audio.
    assert hub.published == []
