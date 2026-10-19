from datetime import datetime, timezone

from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore


def test_legacy_action_trace_is_moved_out_of_event_metadata(tmp_path):
    path = tmp_path / "x.db"
    store = SQLiteStore(path)
    source = store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=datetime.now(timezone.utc), content="你好"))
    store.append_event(Event(character_id="rin", event_type=EventType.ACTION, event_time=source.event_time, content="REPLY", metadata={"source_event_id": source.id, "reason": "test", "trace": {"source_event_id": source.id, "action": {"type": "REPLY", "message": "你好"}}}))
    store.close()

    reopened = SQLiteStore(path)
    try:
        trace = reopened.get_runtime_trace(source.id)
        assert trace["action"]["message"] == "你好"
        action = [event for event in reopened.list_events("rin") if event.event_type == EventType.ACTION][0]
        assert "trace" not in action.metadata
        assert action.metadata["reason"] == "test"
    finally:
        reopened.close()


def test_malformed_legacy_trace_source_id_does_not_block_startup(tmp_path):
    path = tmp_path / "invalid-trace.db"
    store = SQLiteStore(path)
    store.append_event(Event(
        character_id="rin",
        event_type=EventType.ACTION,
        event_time=datetime.now(timezone.utc),
        content="REPLY",
        metadata={"trace": {"source_event_id": "not-an-int"}},
    ))
    store.close()

    # Both the one-time core migration and the incremental compatibility scan
    # must skip the damaged row instead of aborting every store construction.
    for _ in range(2):
        reopened = SQLiteStore(path)
        try:
            actions = [
                event for event in reopened.list_events("rin")
                if event.event_type == EventType.ACTION
            ]
            assert len(actions) == 1
            assert actions[0].metadata["trace"]["source_event_id"] == "not-an-int"
        finally:
            reopened.close()
