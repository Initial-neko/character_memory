import json
import uuid

import httpx
from pydantic import ValidationError

from character_memory.domain.models import ActionDecision, ActionType, PersonReaction
from character_memory.llm.client import OpenAICompatibleModel, ProviderHTTPError


def test_action_message_contract():
    try:
        ActionDecision(type=ActionType.MESSAGE)
    except ValueError:
        pass
    else:
        raise AssertionError("MESSAGE without message must be rejected")


def test_sparse_person_reaction_is_valid_and_silent():
    result = PersonReaction.model_validate({"actions": []})
    assert result.actions == []
    assert result.action.type == ActionType.NO_REPLY
    assert result.action.reason == ""
    assert result.perception == ""
    assert result.reaction == ""
    assert result.mental_state_update == ""
    assert result.memory_candidates == []
    assert result.intent_candidates == []


def test_missing_action_contract_is_not_interpreted_as_silence():
    try:
        PersonReaction.model_validate({})
    except ValidationError:
        pass
    else:
        raise AssertionError("{} must be invalid; explicit silence is actions=[]")


def test_legacy_single_action_is_normalized_to_actions():
    result = PersonReaction.model_validate({"action": {"type": "REPLY", "message": "嗯"}})
    assert len(result.actions) == 1
    assert result.actions[0].message == "嗯"
    assert result.action.message == "嗯"


def test_multi_action_uses_first_action_for_legacy_compatibility():
    result = PersonReaction.model_validate({"actions": [
        {"type": "MESSAGE", "message": "等等"},
        {"type": "EMOJI", "message": "🥺"},
    ]})
    assert [action.type for action in result.actions] == [ActionType.MESSAGE, ActionType.EMOJI]
    assert result.action.type == ActionType.MESSAGE
    assert result.action.message == "等等"


def test_structured_output_retries_once():
    model = OpenAICompatibleModel("key", attempts=2)
    replies = iter(["{}", json.dumps({"actions": []}, ensure_ascii=False)])
    model._request = lambda messages, **kwargs: next(replies)
    try:
        result = model.react("context")
        assert result.actions == []
        assert result.action.type == ActionType.NO_REPLY
        assert model.last_attempt == 2
    finally:
        model.close()


def test_structured_call_uses_json_object_response_format():
    captured = {}

    def handler(request: httpx.Request):
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"actions":[]}'}}]},
        )

    model = OpenAICompatibleModel("key", attempts=1)
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    try:
        result = model.react("context")
        assert result.actions == []
        assert captured["response_format"] == {"type": "json_object"}
        system = captured["messages"][0]["content"]
        assert "model_json_schema" not in system
        assert '"properties"' not in system
        assert "actions" in system
        assert "0 到 3" in system
        assert "actions 必须可以是空数组" in system
        assert "不要刻意惜字" in system
        assert "隐藏思维链" in system
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
