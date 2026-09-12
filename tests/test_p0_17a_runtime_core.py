from __future__ import annotations

from datetime import datetime, timedelta, timezone

from character_memory.avatar_intent import AvatarSearchIntent
from character_memory.domain.models import DiaryResult, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel
from character_memory.storage.sqlite import SQLiteStore


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

    assert isinstance(result, PersonReaction)
    assert result.actions == []
    repair = calls[1][-1]["content"]
    assert "PersonReaction" in repair
    assert "actions" in repair
    assert "STICKER" in repair


def test_generic_retry_names_diary_schema_instead_of_actions_contract():
    model = OpenAICompatibleModel("test-key", attempts=2)
    calls = []
    responses = iter([
        '{"diary":123,"memory_candidates":"bad"}',
        '{"diary":"今天很平静。","mental_state_update":"","memory_candidates":[]}',
    ])

    def fake_request(messages, **_kwargs):
        calls.append(messages)
        return next(responses)

    model._request = fake_request
    try:
        result = model.structured_for_session("write diary", DiaryResult, "diary:test")
    finally:
        model.close()

    assert result.diary == "今天很平静。"
    repair = calls[1][-1]["content"]
    assert "DiaryResult" in repair
    assert "diary" in repair
    assert "memory_candidates" in repair
    assert "actions 为 0~3" not in repair


def test_mental_state_history_writes_only_real_changes(tmp_path):
    store = SQLiteStore(tmp_path / "mental-state.db")
    t0 = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
    try:
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
    finally:
        store.close()
