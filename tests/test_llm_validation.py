import json
import uuid

import httpx

from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel, ProviderHTTPError


def test_action_message_contract():
    try:
        ActionDecision(type=ActionType.REPLY, reason="回应")
    except ValueError:
        pass
    else:
        raise AssertionError("REPLY without message must be rejected")


def test_sparse_person_reaction_is_valid():
    result = PersonReaction.model_validate({"action": {"type": "NO_REPLY"}})
    assert result.action.type == ActionType.NO_REPLY
    assert result.action.reason == ""
    assert result.perception == ""
    assert result.reaction == ""
    assert result.mental_state_update == ""
    assert result.memory_candidates == []
    assert result.intent_candidates == []


def test_structured_output_retries_once():
    model = OpenAICompatibleModel("key", attempts=2)
    replies = iter(["not json", json.dumps({"action": {"type": "NO_REPLY"}}, ensure_ascii=False)])
    model._request = lambda messages, **kwargs: next(replies)
    try:
        result = model.react("context")
        assert result.action.type == ActionType.NO_REPLY
    finally:
        model.close()


def test_structured_call_uses_json_object_response_format():
    captured = {}

    def handler(request: httpx.Request):
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"action":{"type":"NO_REPLY"}}'}}]},
        )

    model = OpenAICompatibleModel("key", attempts=1)
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        result = model.react("context")
        assert result.action.type == ActionType.NO_REPLY
        assert captured["response_format"] == {"type": "json_object"}
        system = captured["messages"][0]["content"]
        assert "model_json_schema" not in system
        assert '"properties"' not in system
        assert "不要刻意惜字" in system
        assert "自然追问" in system
    finally:
        model.close()


def test_provider_error_body_is_visible_without_request_headers():
    response = httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "Model is unavailable"}})
    body = OpenAICompatibleModel._error_body(response)
    error = ProviderHTTPError(400, "https://example.test/v1/chat/completions", body, "req-123")
    text = str(error)
    assert "400" in text
    assert "Model is unavailable" in text
    assert "req-123" in text
    assert "Authorization" not in text


def test_opencode_headers_use_stable_conversation_session():
    model = OpenAICompatibleModel("key", session_id="11111111-2222-3333-4444-555555555555")
    try:
        default_headers = model._headers(include_session=True)
        conversation_headers_1 = model._headers(include_session=True, conversation_id="rin:web")
        conversation_headers_2 = model._headers(include_session=True, conversation_id="rin:web")
        discovery_headers = model._headers(include_session=False)
        assert default_headers["x-opencode-session"] == "11111111-2222-3333-4444-555555555555"
        assert conversation_headers_1["x-opencode-session"] == conversation_headers_2["x-opencode-session"]
        assert uuid.UUID(conversation_headers_1["x-opencode-session"])
        assert conversation_headers_1["x-opencode-client"] == "character-memory"
        assert conversation_headers_1["User-Agent"].startswith("character-memory/")
        assert "x-opencode-session" not in discovery_headers
        assert "Authorization" in discovery_headers
    finally:
        model.close()
