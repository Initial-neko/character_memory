from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.application.wake_service import CharacterWakeService
from character_memory.avatar_intent import AvatarSearchIntent
from character_memory.domain.models import ActionDecision, ActionType, EventType, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_structured_retry_is_schema_aware_for_avatar_intent():
    model = OpenAICompatibleModel("test-key", attempts=2)
    calls = []
    responses = iter([
        '{"visual_intent":"近景头像","queries":[]}',
        '{"visual_intent":"近景头像","queries":["anime girl calm portrait"],"preferred_mood":"平静","preferred_style":"近景"}',
    ])

    def fake_request(messages, **_kwargs):
        calls.append(messages)
        return next(responses)

    model._request = fake_request
    try:
        result = model.structured_for_session("plan avatar", AvatarSearchIntent, "avatar:test")
    finally:
        model.close()

    assert result.queries == ["anime girl calm portrait"]
    assert len(calls) == 2
    repair = calls[1][-1]["content"]
    assert "AvatarSearchIntent" in repair
    assert "visual_intent" in repair
    assert "queries" in repair
    assert "actions 为 0~3" not in repair


def test_person_reaction_retry_keeps_action_specific_contract():
    model = OpenAICompatibleModel("test-key", attempts=2)
    calls = []
    responses = iter([
        '{"perception":"看到了"}',
        '{"perception":"看到了","actions":[],"memory_candidates":[],"intent_candidates":[]}',
    ])

    def fake_request(messages, **_kwargs):
        calls.append(messages)
        return next(responses)

    model._request = fake_request
    try:
        result = model.react_for_session("context", "direct:test")
    finally:
        model.close()

    assert result.actions == []
    repair = calls[1][-1]["content"]
    assert "PersonReaction" in repair
    assert "actions" in repair
    assert "STICKER" in repair


def test_mental_state_history_writes_only_real_changes(tmp_path):
    store = SQLiteStore(tmp_path / "mental-state.db")
    t0 = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
    assert store.set_mental_state("rin", "平静", t0, 1) is True
    assert store.set_mental_state("rin", "  平静  ", t0 + timedelta(minutes=1), 2) is False
    assert store.set_mental_state("rin", "有点担心", t0 + timedelta(minutes=2), 3) is True

    with store._lock:
        rows = store.conn.execute(
            "SELECT content,source_event_id FROM mental_state_history WHERE character_id=? ORDER BY id",
            ("rin",),
        ).fetchall()
    assert [(row["content"], row["source_event_id"]) for row in rows] == [
        ("平静", 1),
        ("有点担心", 3),
    ]
    assert store.get_mental_state("rin") == "有点担心"
    store.close()


class _RecordingRuntime:
    def __init__(self, result):
        self.result = result
        self.events = []

    def handle(self, event, **_kwargs):
        event = event.model_copy(update={"id": event.id or 77})
        self.events.append(event)
        self.result.event = event
        return self.result


def test_chat_service_wake_is_time_tick_and_does_not_force_message(tmp_path):
    store = SQLiteStore(tmp_path / "wake-chat.db")
    result = SimpleNamespace(
        event=None,
        reaction=PersonReaction(actions=[]),
        created_intent_ids=[],
    )
    runtime = _RecordingRuntime(result)
    now = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
    chat = ChatService(store, {"rin": runtime}, FixedClock(now))

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
    assert "必须回复" in event.content
    store.close()


def test_wake_service_60_min_gate_and_manual_force(tmp_path):
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

    bundle = SimpleNamespace(chat=FakeChat())
    service = CharacterWakeService(
        store,
        lambda: bundle,
        lambda: [{"id":"rin"}],
        interval_minutes=60,
    )
    t0 = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)
    service.prime(t0)

    assert service.is_due("rin", t0 + timedelta(minutes=59, seconds=59)) is False
    assert service.is_due("rin", t0 + timedelta(minutes=60)) is True
    periodic = service.wake("rin", reason="PERIODIC", at=t0 + timedelta(minutes=60))
    assert periodic is not None
    assert periodic.reason == "PERIODIC"
    assert len(calls) == 1

    # Manual wake intentionally bypasses the 60-minute interval.
    manual = service.wake(
        "rin",
        reason="MANUAL",
        at=t0 + timedelta(minutes=61),
        conversation_id="manual-conversation",
        force=True,
    )
    assert manual is not None
    assert manual.conversation_id == "manual-conversation"
    assert calls[-1][2] == "MANUAL"
    store.close()


def test_p0_17_frontend_contracts_and_javascript_syntax():
    hardening = (WEB / "runtime_hardening.js").read_text(encoding="utf-8")
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")

    assert "/v1/characters/${encodeURIComponent(characterId)}/wake" in hardening
    assert 'source.addEventListener("open"' in hardening
    assert "/v1/chat/history-page" in hardening
    assert "new Map(CM.state.directHistory.messages" in hardening
    assert "高级调试" in hardening
    assert "Compiled Context" not in hardening
    assert 'source.addEventListener("open"' in groups
    assert "reconcileLatest(groupId)" in groups
    assert "/static/runtime_hardening.js" in index
    assert "/static/p0_17.css" in index

    node = shutil.which("node")
    if node:
        for path in (WEB / "runtime_hardening.js", WEB / "groups.js"):
            subprocess.run([node, "--check", str(path)], check=True, capture_output=True, text=True)
