import json

import httpx

from character_memory.domain.models import ActionDecision, ActionType
from character_memory.llm.client import OpenAICompatibleModel, ProviderHTTPError


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


def test_provider_error_body_is_visible_without_request_headers():
    response = httpx.Response(
        400,
        json={"error": {"type": "invalid_request_error", "message": "Model is unavailable"}},
    )
    body = OpenAICompatibleModel._error_body(response)
    error = ProviderHTTPError(400, "https://example.test/v1/chat/completions", body, "req-123")

    text = str(error)
    assert "400" in text
    assert "Model is unavailable" in text
    assert "req-123" in text
    assert "Authorization" not in text


def test_opencode_chat_headers_have_stable_session_and_client_identity():
    model = OpenAICompatibleModel("key", session_id="11111111-2222-3333-4444-555555555555")

    chat_headers = model._headers(include_session=True)
    discovery_headers = model._headers(include_session=False)

    assert chat_headers["x-opencode-session"] == "11111111-2222-3333-4444-555555555555"
    assert chat_headers["x-opencode-client"] == "character-memory"
    assert chat_headers["User-Agent"].startswith("character-memory/")
    assert "x-opencode-session" not in discovery_headers
    assert "Authorization" in discovery_headers
