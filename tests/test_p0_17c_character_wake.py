from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.application.wake_service import CharacterWakeService
from character_memory.domain.models import EventType, PersonReaction
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


class RecordingRuntime:
    def __init__(self):
        self.events = []

    def handle(self, event, **_kwargs):
        stored = event.model_copy(update={"id": event.id or 77})
        self.events.append(stored)
        return SimpleNamespace(
            event=stored,
            reaction=PersonReaction(actions=[]),
            created_intent_ids=[],
        )


def test_chat_service_dispatch_wake_is_time_tick_and_never_forces_a_message(tmp_path):
    store = SQLiteStore(tmp_path / "wake-chat.db")
    runtime = RecordingRuntime()
    now = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
    chat = ChatService(store, {"rin": runtime}, FixedClock(now))
    try:
        output = chat.dispatch_wake(
            character_id="rin",
            at=now,
            reason="MANUAL",
            conversation_id="wake-conversation",
        )
        assert output.reaction.actions == []
        assert len(runtime.events) == 1
        event = runtime.events[0]
        assert event.event_type == EventType.TIME_TICK
        assert event.metadata["wake_reason"] == "MANUAL"
        assert event.metadata["conversation_id"] == "wake-conversation"
        assert "不是要求你必须回复" in event.content
    finally:
        store.close()


def test_periodic_wake_is_due_at_60_minutes_and_manual_bypasses_interval(tmp_path):
    store = SQLiteStore(tmp_path / "wake-service.db")
    calls = []

    class FakeChat:
        def dispatch_wake(self, *, character_id, at, reason, conversation_id=None):
            calls.append((character_id, at, reason, conversation_id))
            return SimpleNamespace(
                event=SimpleNamespace(id=91, metadata={"conversation_id": conversation_id or "rin:proactive"}),
                reaction=PersonReaction(actions=[]),
                created_intent_ids=[],
            )

    service = CharacterWakeService(
        store,
        lambda: SimpleNamespace(chat=FakeChat()),
        lambda: [{"id":"rin"}],
        interval_minutes=60,
    )
    t0 = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)
    try:
        service.prime(t0)
        assert service.is_due("rin", t0 + timedelta(minutes=59, seconds=59)) is False
        assert service.is_due("rin", t0 + timedelta(minutes=60)) is True

        periodic = service.wake("rin", reason="PERIODIC", at=t0 + timedelta(minutes=60))
        assert periodic is not None
        assert periodic.reason == "PERIODIC"
        assert periodic.silent is True
        assert len(calls) == 1

        # The periodic call just marked the character, but manual Wake is an
        # explicit user action and deliberately bypasses the interval.
        manual = service.wake(
            "rin",
            reason="MANUAL",
            at=t0 + timedelta(minutes=61),
            conversation_id="manual-conversation",
            force=True,
        )
        assert manual is not None
        assert manual.reason == "MANUAL"
        assert manual.conversation_id == "manual-conversation"
        assert calls[-1][2] == "MANUAL"
    finally:
        store.close()


def test_failed_periodic_wake_marks_interval_before_provider_call(tmp_path):
    store = SQLiteStore(tmp_path / "wake-failure.db")

    class FailingChat:
        def dispatch_wake(self, **_kwargs):
            raise RuntimeError("provider down")

    service = CharacterWakeService(
        store,
        lambda: SimpleNamespace(chat=FailingChat()),
        lambda: [{"id":"rin"}],
        interval_minutes=60,
    )
    t0 = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
    try:
        service.prime(t0)
        at = t0 + timedelta(minutes=60)
        try:
            service.wake("rin", reason="PERIODIC", at=at)
        except RuntimeError as exc:
            assert "provider down" in str(exc)
        else:
            raise AssertionError("wake should propagate provider failure")
        assert service.is_due("rin", at + timedelta(seconds=30)) is False
    finally:
        store.close()


def test_wake_frontend_contract_and_javascript_syntax():
    script = (WEB / "wake.js").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")

    assert 'button.id = "wakeButton"' in script
    assert "/v1/characters/${encodeURIComponent(characterId)}/wake" in script
    assert "CM.connectDirectStream()" in script
    assert "result.silent" in script
    assert "button.hidden = group" in script
    assert '/static/wake.js' in index

    node = shutil.which("node")
    if node:
        subprocess.run([node, "--check", str(WEB / "wake.js")], check=True, capture_output=True, text=True)
