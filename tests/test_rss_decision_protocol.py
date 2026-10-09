import json

import httpx
import pytest
from pydantic import ValidationError

from character_memory.domain.models import EventType
from character_memory.llm.client import OpenAICompatibleModel, StructuredOutputError
from character_memory.world_activity import PersonalWorldReadPlan
from test_rss_world_perception import consumer_fixture
from test_world_activity import NOW


@pytest.mark.parametrize("decision", [
    {"action": "READ_RSS", "item_ids": [2]},
    {"action": "read_rss", "item_ids": [2]},
    {"choice": "READ_RSS", "item_ids": [2]},
])
def test_explicit_read_decision_preserves_selected_ids(decision):
    plan = PersonalWorldReadPlan.model_validate(decision)
    assert plan.choice == "READ_RSS" and plan.item_ids == [2] and plan.browse


@pytest.mark.parametrize("decision", [
    {}, {"item_ids": [2]}, {"action": "MESSAGE"},
    {"choice": "NO_ACTION", "action": "READ_RSS", "item_ids": [2]},
    {"choice": "WEB_SEARCH", "query": ""},
])
def test_missing_conflicting_or_invalid_decision_is_not_silence(decision):
    with pytest.raises(ValidationError):
        PersonalWorldReadPlan.model_validate(decision)


@pytest.mark.parametrize("decision", [{"choice": "NO_ACTION"}, {"action": "NO_ACTION"}])
def test_explicit_silence_remains_a_valid_decision(decision):
    plan = PersonalWorldReadPlan.model_validate(decision)
    assert plan.choice == "NO_ACTION" and not plan.browse and not plan.item_ids


def provider_fixture(access, replies):
    requests = []
    replies = iter(replies)

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(next(replies))}}]})

    model = OpenAICompatibleModel("isolated-mock-key", attempts=1)
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(respond))
    access.require_bundle().model = model
    return model, requests


def test_actual_client_alias_reaches_local_read_appraisal_and_observation(tmp_path):
    store, access, _, _, _, reading, service = consumer_fixture(tmp_path)
    item_id = reading.candidates("c00")[0]["item_id"]
    model, requests = provider_fixture(access, [
        {"action": "READ_RSS", "item_ids": [item_id]},
        {"items": [{"item_id": item_id, "keep": True, "summary": "Read the actual local feed", "personal_note": "Worth considering"}]},
    ])
    access.world_observer.observe = lambda *a, **kw: pytest.fail("RSS cannot fetch Web")
    try:
        result = service.browse_character("c00", now=NOW, opportunity_id="provider-alias-read")
        assert result["reading_choice"] == "READ_RSS" and result["execution_status"] == "SUCCESS"
        events = store.list_events("c00")
        assert len(events) == 1 and events[0].event_type == EventType.WORLD_OBSERVATION
        assert events[0].metadata["rss_reading"]["item_id"] == item_id
        assert not store.list_memories("c00")
        assert len(requests) == 2  # one plan + one batch appraisal, no repair
        assert service.browse_character("c00", now=NOW, opportunity_id="provider-alias-read") == result
        assert len(requests) == 2
    finally:
        model.close()
        store.close()


def test_actual_client_missing_choice_records_failure_not_no_action(tmp_path):
    store, access, _, _, _, reading, service = consumer_fixture(tmp_path)
    model, requests = provider_fixture(access, [{"item_ids": [reading.candidates("c00")[0]["item_id"]]}])
    try:
        with pytest.raises(StructuredOutputError):
            service.browse_character("c00", now=NOW, opportunity_id="provider-missing-choice")
        receipt = service.repository.get_browse_decision("provider-missing-choice")
        assert receipt["phase"] == "FAILED" and receipt["result"]["opportunity_status"] == "FAILED"
        assert not store.list_events("c00") and not store.list_memories("c00")
        assert len(requests) == 1 and len(reading.candidates("c00")) == 3
    finally:
        model.close()
        store.close()


@pytest.mark.parametrize('decision,choice',[
    ({'choice':'NO_ACTION','action':None},'NO_ACTION'),
    ({'choice':None,'action':'READ_RSS','item_ids':[2]},'READ_RSS'),
    ({'browse':True,'query':'public topic'},'WEB_SEARCH'),
    ({'browse':False,'query':''},'NO_ACTION'),
])
def test_nullable_alias_and_legacy_boolean_are_locally_compatible(decision,choice):
    assert PersonalWorldReadPlan.model_validate(decision).choice==choice


@pytest.mark.parametrize('decision',[
    {'browse':'true','query':'topic'}, {'browse':1,'query':'topic'},
    {'browse':True,'query':''}, {'choice':'MESSAGE','browse':False},
    {'choice':'NO_ACTION','action':'WEB_SEARCH','query':'topic'},
])
def test_compatibility_does_not_convert_invalid_or_conflicting_decisions(decision):
    with pytest.raises(ValidationError):PersonalWorldReadPlan.model_validate(decision)
