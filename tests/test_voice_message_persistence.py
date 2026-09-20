from datetime import datetime, timezone
from pathlib import Path

from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.voice_message_fields import (
    VOICE_DURATION_MS,
    VOICE_ERROR,
    VOICE_MEDIA_ID,
    VOICE_STATUS,
    voice_fields,
    voice_pending_fields,
)


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "voice.db")


def _voice_event(character_id="momo") -> Event:
    return Event(
        character_id=character_id,
        event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content="晚上好呀",
        metadata={"action": "VOICE_MESSAGE", "action_index": 0, "voice_status": "pending"},
    )


def test_get_event_round_trips_a_persisted_event(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    reloaded = store.get_event(saved.id)

    assert reloaded.id == saved.id
    assert reloaded.content == "晚上好呀"
    assert reloaded.metadata["voice_status"] == "pending"


def test_get_event_returns_none_for_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.get_event(999999) is None


def test_update_event_metadata_replaces_the_document(tmp_path):
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    updated = store.update_event_metadata(
        saved.id,
        {
            "action": "VOICE_MESSAGE",
            "action_index": 0,
            "voice_status": "ready",
            "voice_media_id": "abc123",
            "voice_duration_ms": 1840,
        },
    )

    assert updated is True
    reloaded = store.get_event(saved.id)
    assert reloaded.metadata["voice_status"] == "ready"
    assert reloaded.metadata["voice_media_id"] == "abc123"
    assert reloaded.metadata["voice_duration_ms"] == 1840


def test_update_event_metadata_reports_a_missing_row(tmp_path):
    store = _store(tmp_path)

    assert store.update_event_metadata(999999, {"voice_status": "ready"}) is False


def test_update_event_metadata_leaves_the_row_readable(tmp_path):
    """event_time_epoch is filtered on every read; an UPDATE must not clear it."""
    store = _store(tmp_path)
    saved = store.append_event(_voice_event())

    store.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "failed"})

    row = store.conn.execute("SELECT event_time_epoch FROM events WHERE id=?", (saved.id,)).fetchone()
    assert row["event_time_epoch"] is not None
    assert store.get_event(saved.id) is not None


def test_group_update_event_metadata_replaces_the_document(tmp_path):
    """GroupRepository reaches through self.store rather than owning a
    connection of its own, so this is the one place that proxy access could
    silently be written wrong. It also has no caller yet, which is exactly why
    it needs a real test rather than a one-off script."""
    from character_memory.group_store import GroupEvent, GroupRepository

    store = _store(tmp_path)
    repo = GroupRepository(store)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    group = repo.create_group("测试群", ["momo", "rin"], now)
    saved = repo.append_event(
        GroupEvent(
            conversation_id=group.id,
            turn_id="turn-1",
            actor_type="CHARACTER",
            actor_id="momo",
            event_type="CHARACTER_MESSAGE",
            event_time=now,
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", "voice_status": "pending"},
        )
    )

    assert repo.update_event_metadata(saved.id, {"action": "VOICE_MESSAGE", "voice_status": "ready"}) is True
    assert repo.update_event_metadata(999999, {"voice_status": "ready"}) is False

    # list_events filters on event_time_epoch IS NOT NULL, so reading the row
    # back proves the UPDATE did not clear that column.
    events = repo.list_events(group.id)
    assert events[-1].metadata["voice_status"] == "ready"


ROOT = Path(__file__).resolve().parents[1]


def test_voice_pending_fields_are_the_four_canonical_keys():
    assert voice_pending_fields() == {
        VOICE_STATUS: "pending",
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_voice_fields_reads_a_ready_message():
    assert voice_fields(
        {
            "action": "VOICE_MESSAGE",
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: "abc123",
            VOICE_DURATION_MS: 1840,
        }
    ) == {
        VOICE_STATUS: "ready",
        VOICE_MEDIA_ID: "abc123",
        VOICE_DURATION_MS: 1840,
        VOICE_ERROR: None,
    }


def test_voice_fields_defaults_to_none_for_a_plain_text_message():
    assert voice_fields({"action": "MESSAGE"}) == {
        VOICE_STATUS: None,
        VOICE_MEDIA_ID: None,
        VOICE_DURATION_MS: None,
        VOICE_ERROR: None,
    }


def test_both_history_payloads_read_the_shared_keys():
    """Two payload builders read these keys. A literal string in either one is
    the drift this module exists to prevent, so assert they call the shared
    reader rather than trusting review to catch it."""
    for relative in (
        "src/character_memory/history_web.py",
        "src/character_memory/group_web.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "voice_fields(" in source, relative


def test_both_runtimes_write_the_pending_state_from_the_shared_helper():
    """Same reasoning for the write side: the two action loops are verbatim
    copies, and a hardcoded key in one of them drifts on the next rename."""
    for relative in (
        "src/character_memory/runtime/person_runtime.py",
        "src/character_memory/application/group_conversation_service.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "voice_pending_fields(" in source, relative


def test_a_persisted_voice_message_reloads_with_its_audio_reference(tmp_path):
    """The whole point of this task: write pending, flip to ready, and confirm a
    history read still carries the asset reference. Without the payload spread in
    Steps 5-6 these keys vanish on reload and the bubble loses its audio."""
    store = _store(tmp_path)
    saved = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
            content="晚上好呀",
            metadata={"action": "VOICE_MESSAGE", "action_index": 0, **voice_pending_fields()},
        )
    )

    assert store.get_event(saved.id).metadata[VOICE_STATUS] == "pending"

    metadata = store.get_event(saved.id).metadata
    metadata.update(
        {
            VOICE_STATUS: "ready",
            VOICE_MEDIA_ID: "abc123",
            VOICE_DURATION_MS: 1840,
        }
    )
    store.update_event_metadata(saved.id, metadata)

    payload = voice_fields(store.get_event(saved.id).metadata)
    assert payload[VOICE_STATUS] == "ready"
    assert payload[VOICE_MEDIA_ID] == "abc123"
    assert payload[VOICE_DURATION_MS] == 1840
    assert payload[VOICE_ERROR] is None
