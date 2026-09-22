from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from character_memory.application.proactive_service import ProactiveService
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, PersonReaction
from character_memory.storage.sqlite import SQLiteStore


class FakeChat:
    def __init__(self, reaction: PersonReaction | None = None, error: Exception | None = None):
        self.reaction = reaction or PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="到了吗？")])
        self.error = error
        self.calls = []

    def dispatch_proactive_intent(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(reaction=self.reaction, event=SimpleNamespace(id=99))


def _intent(store, character_id: str, now: datetime, *, earliest_delta=-1, expires_delta=4):
    return store.add_intent(
        character_id,
        "问问用户今天的汇报怎么样了",
        ActionType.PROACTIVE_MESSAGE.value,
        now - timedelta(hours=2),
        now + timedelta(hours=earliest_delta),
        now + timedelta(hours=expires_delta),
    )


def test_dispatches_only_due_intents_and_marks_executed(tmp_path):
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    due_id = _intent(store, "momo", now)
    _intent(store, "momo", now, earliest_delta=2, expires_delta=6)
    chat = FakeChat()
    service = ProactiveService(store, chat)

    assert service.has_due(["momo"], now) is True
    outcomes = service.dispatch_due(["momo"], now)

    assert [item["intent_id"] for item in outcomes] == [due_id]
    assert outcomes[0]["status"] == "EXECUTED"
    assert chat.calls[0]["character_id"] == "momo"
    rows = {row["id"]: row["status"] for row in store.list_intents("momo")}
    assert rows[due_id] == "EXECUTED"
    assert "PENDING" in rows.values()
    store.close()


def test_silence_suppresses_due_intent(tmp_path):
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    intent_id = _intent(store, "rei", now)
    chat = FakeChat(PersonReaction(actions=[]))

    outcomes = ProactiveService(store, chat).dispatch_due(["rei"], now)

    assert outcomes[0]["status"] == "SUPPRESSED"
    row = next(row for row in store.list_intents("rei") if row["id"] == intent_id)
    assert row["status"] == "SUPPRESSED"
    store.close()


def test_dispatch_error_does_not_retry_forever(tmp_path):
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    intent_id = _intent(store, "haru", now)
    chat = FakeChat(error=RuntimeError("provider down"))
    service = ProactiveService(store, chat)

    outcomes = service.dispatch_due(["haru"], now)

    assert outcomes[0]["status"] == "ERROR"
    assert service.has_due(["haru"], now) is False
    row = next(row for row in store.list_intents("haru") if row["id"] == intent_id)
    assert row["status"] == "ERROR"
    store.close()


def test_unanswered_proactive_message_blocks_another_proactive_turn(tmp_path):
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    _intent(store, "momo", now)
    store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=now - timedelta(minutes=10),
            content="汇报结束了吗？",
            metadata={
                "source_event_id": 10,
                "source_event_type": EventType.PROACTIVE_INTENT.value,
                "action": ActionType.MESSAGE.value,
            },
        )
    )
    chat = FakeChat()
    service = ProactiveService(store, chat)

    assert service.has_due(["momo"], now) is False
    assert service.dispatch_due(["momo"], now) == []
    assert chat.calls == []
    assert any(row["status"] == "PENDING" for row in store.list_intents("momo"))
    store.close()


def test_background_proactive_dispatch_reuses_direct_voice_publisher():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "character_memory" / "api.py").read_text(encoding="utf-8")

    assert "scheduler.publish_direct_responses(" in source
    assert 'source_event.metadata.get("conversation_id")' in source
