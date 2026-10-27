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


def test_structured_call_trace_keeps_the_rejected_output_of_a_repair():
    """A repaired call must be distinguishable from a clean one.

    The generic repair prompt resolves an invalid field by asking for empty
    values, so the caller cannot tell a deliberate empty list from one that was
    dropped during repair unless the trace keeps the first output.
    """
    model = OpenAICompatibleModel("key", attempts=2)
    replies = iter([
        json.dumps({"actions": [{"type": "MESSAGE"}]}, ensure_ascii=False),
        json.dumps({"actions": []}, ensure_ascii=False),
    ])
    model._request = lambda messages, **kwargs: next(replies)
    try:
        result = model.structured_call_for_session("context", PersonReaction, "s1")
    finally:
        model.close()

    assert result.value.actions == []
    assert result.trace.attempt == 2
    assert '"MESSAGE"' in result.trace.rejected_response_text
    assert result.trace.validation_error
    assert result.trace.response_text == json.dumps({"actions": []}, ensure_ascii=False)


def test_structured_call_trace_has_no_rejected_output_when_the_first_attempt_validates():
    model = OpenAICompatibleModel("key", attempts=2)
    model._request = lambda messages, **kwargs: json.dumps({"actions": []})
    try:
        result = model.structured_call_for_session("context", PersonReaction, "s1")
    finally:
        model.close()

    assert result.trace.attempt == 1
    assert result.trace.rejected_response_text == ""
    assert result.trace.validation_error == ""


def test_source_fidelity_reaches_provider_for_person_and_presentation_subclass():
    class PresentedReaction(PersonReaction):
        pass
    for schema in (PersonReaction, PresentedReaction):
        captured=[]
        model=OpenAICompatibleModel('fixture-key',attempts=1)
        def request(messages, **kwargs):
            captured.append(messages)
            return '{"actions":[]}'
        model._request=request
        try:
            result=model._call_result('unchanged persona/context',schema)
            assert len(captured)==1 and result.value.actions==[]
            system=captured[0][0]
            assert system['role']=='system'
            assert system['content'].startswith('事实准确性优先于人物化修辞和叙事完整。')
            assert '偏好或愿望不是已做过' in system['content']
            assert '当前用户给出的原文可以引用' in system['content']
            assert '不增删字符、标点或额外回应' in system['content']
            assert captured[0][1]=={'role':'user','content':'unchanged persona/context'}
        finally:
            model.close()


def test_non_person_structured_system_does_not_gain_person_fidelity_policy():
    from pydantic import BaseModel
    class Other(BaseModel):
        value: str
    model=OpenAICompatibleModel('fixture-key',attempts=1)
    try:
        messages=model.preview_messages('non-person contract',Other)
        assert messages[0]['content']=='根据输入返回符合目标对象语义的 JSON 对象，不要添加 JSON 之外的解释。'
        assert messages[1]['content']=='non-person contract'
    finally:
        model.close()

def test_person_source_contract_keeps_unknown_user_identity_neutral():
    system=OpenAICompatibleModel._system_prompt(PersonReaction)
    assert 'Persona只描述人物本人' in system
    assert '不能推导用户的性别' in system
    assert '记忆候选' in system and '用户或你' in system

def test_retired_opencode_alias_is_canonical_before_the_only_request():
    model=OpenAICompatibleModel('fixture-key',model='deepseek-flash',vision_model='deepseek-flash',attempts=1)
    try:
        assert model.model=='deepseek-v4.1-flash'
        assert model.vision_model=='deepseek-v4.1-flash'
        assert model.last_model=='deepseek-v4.1-flash'
    finally:model.close()


def test_retired_alias_does_not_change_other_providers_or_custom_vision():
    other=OpenAICompatibleModel('fixture-key',model='deepseek-flash',base_url='https://other.example/v1',vision_model='private-vision',attempts=1)
    official=OpenAICompatibleModel('fixture-key',model='deepseek-flash',vision_model='private-vision',attempts=1)
    try:
        assert other.model=='deepseek-flash' and other.vision_model=='private-vision'
        assert official.model=='deepseek-v4.1-flash' and official.vision_model=='private-vision'
    finally:other.close();official.close()

def test_retired_alias_sends_one_canonical_wire_request():
    captured=[]
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"actions":[]}'}}]})
    model=OpenAICompatibleModel('fixture-key',model='deepseek-flash',attempts=1)
    model.client.close();model.client=httpx.Client(transport=httpx.MockTransport(handle))
    try:
        result=model.react_call_for_session('hello','alias-wire-test')
        assert len(captured)==1
        assert captured[0]['model']=='deepseek-v4.1-flash'
        assert result.trace.model=='deepseek-v4.1-flash'
    finally:model.close()