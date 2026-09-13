from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from character_memory.application.clock import FixedClock
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.domain.models import ActionType, PersonReaction
from character_memory.storage.sqlite import SQLiteStore


def test_optional_memory_drift_does_not_invalidate_a_valid_outward_reply():
    reaction = PersonReaction.model_validate(
        {
            "actions": [{"type": "MESSAGE", "message": "我刚刚手滑了一下（笑）"}],
            "memory_candidates": [
                {"content": "用户提到了一个值得记住的偏好", "memory_type": "USER", "importance": 4},
                {"importance": 0.8},  # malformed optional candidate: no content
            ],
            "intent_candidates": [],
        }
    )

    assert len(reaction.actions) == 1
    assert reaction.actions[0].type == ActionType.MESSAGE
    assert reaction.actions[0].message == "我刚刚手滑了一下（笑）"
    assert len(reaction.memory_candidates) == 1
    assert reaction.memory_candidates[0].importance == 1.0
    assert reaction.memory_candidates[0].content == "用户提到了一个值得记住的偏好"


def test_optional_intent_drift_is_bounded_instead_of_breaking_the_reply():
    reaction = PersonReaction.model_validate(
        {
            "actions": [{"type": "MESSAGE", "message": "晚点再说也行。"}],
            "memory_candidates": [],
            "intent_candidates": [
                {
                    "content": "之后再问问",
                    "preferred_action": "NOT_A_REAL_ACTION",
                    "earliest_hours": "800",
                    "expires_hours": -2,
                }
            ],
        }
    )

    assert reaction.actions[0].message == "晚点再说也行。"
    assert len(reaction.intent_candidates) == 1
    intent = reaction.intent_candidates[0]
    assert intent.preferred_action == ActionType.PROACTIVE_MESSAGE
    assert intent.earliest_hours == 720
    assert intent.expires_hours == 720


def test_outward_action_contract_remains_strict():
    with pytest.raises(ValidationError):
        PersonReaction.model_validate(
            {
                "actions": [{"type": "MESSAGE", "message": ""}],
                "memory_candidates": [{"content": "optional", "importance": 4}],
            }
        )


def test_one_group_member_failure_does_not_swallow_later_members(tmp_path):
    store = SQLiteStore(tmp_path / "group-resilience.db")
    now = datetime(2026, 9, 13, 8, 48, tzinfo=timezone.utc)
    service = GroupConversationService(
        store,
        {"rin": object(), "momo": object()},
        FixedClock(now),
        profiles=[{"id": "rin", "name": "Rin"}, {"id": "momo", "name": "Momo"}],
    )
    group = service.create_group("容错群", ["rin", "momo"], at=now)
    source = service.persist_user_event(group.id, "你们怎么看？", at=now)

    called = []
    completed = []

    def fake_react_member(**kwargs):
        character_id = kwargs["character_id"]
        called.append(character_id)
        if character_id == "rin":
            raise RuntimeError("simulated malformed structured output")
        return {
            "character_id": character_id,
            "actions": [{"type": "MESSAGE", "message": "我还在这呢。"}],
            "emitted_event_ids": [],
            "emitted_events": [],
            "created_memory_ids": [],
            "perception": "",
            "reaction": "",
            "model_ms": 1.0,
            "explicitly_mentioned": False,
        }

    service._react_member = fake_react_member
    result = service.react_from_event(source, on_member=completed.append)

    assert called == ["rin", "momo"]
    assert len(result["decisions"]) == 2
    assert result["decisions"][0]["character_id"] == "rin"
    assert result["decisions"][0]["actions"] == []
    assert "simulated malformed structured output" in result["decisions"][0]["error"]
    assert result["decisions"][1]["character_id"] == "momo"
    assert result["decisions"][1]["actions"][0]["message"] == "我还在这呢。"
    assert [item["character_id"] for item in completed] == ["rin", "momo"]

    store.close()
