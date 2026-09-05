import json

from character_memory.domain.models import ActionDecision, ActionType
from character_memory.llm.client import OpenAICompatibleModel


def test_action_message_contract():
    try:
        ActionDecision(type=ActionType.REPLY, reason="回应")
    except ValueError:
        pass
    else:
        raise AssertionError("REPLY without message must be rejected")


def test_structured_output_retries_once():
    model = OpenAICompatibleModel("key", attempts=2)
    replies = iter(
        [
            "not json",
            json.dumps(
                {
                    "perception": "看到了",
                    "reaction": "平静",
                    "mental_state_update": "平静",
                    "action": {"type": "NO_REPLY", "reason": "对话自然结束", "message": None},
                    "memory_candidates": [],
                    "intent_candidates": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    model._request = lambda messages: next(replies)
    result = model.react("context")
    assert result.action.type == ActionType.NO_REPLY
