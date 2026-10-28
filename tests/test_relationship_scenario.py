"""Relationship practice: distinct partner views, explicit failure, no real memory."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from character_memory.application.relationship_scenario import (
    RelationshipScenarioService,
    ScenarioUnavailable,
)
from character_memory.application.reply_suggestion_signals import record_choice
from character_memory.application.reply_suggestions import (
    ReplySuggestionService,
    SuggestionStale,
)
from character_memory.domain.activity import ActivityAction, activity_experience_scope
from character_memory.domain.experience_scope import is_real_experience
from character_memory.domain.facts import FactRef
from character_memory.domain.relationship_scenario import (
    ScenarioCreate,
    ScenarioRoundOutput,
    ScenarioRoundRequest,
)
from character_memory.domain.reply_suggestions import SuggestionRequest
from character_memory.storage.activities import ActivityConflict, ActivityRepository
from character_memory.storage.scenarios import (
    ScenarioConflict,
    ScenarioFailed,
    ScenarioRepository,
)
from character_memory.storage.sqlite import SQLiteStore


REQUEST = "req-scenario-0001"
PARTICIPANTS = [
    {"actor_type": "CHARACTER", "character_id": "rin"},
    {"actor_type": "SIMULATED_CHARACTER", "local_id": "mother", "display_name": "妈妈"},
    {"actor_type": "SIMULATED_CHARACTER", "local_id": "sister", "display_name": "姐姐"},
]
DESCRIPTIONS = {
    "sim:mother": "担心我辞职后没有稳定收入，习惯先问最坏的情况",
    "sim:sister": "务实，愿意帮我想步骤，但不喜欢听空话",
}
VIEWS = {
    "rin": {"stance": "先照顾自己", "perspective": "你已经累了很久，可以先把边界说清楚，不必一次说服所有人。"},
    "sim:mother": {"stance": "担心风险", "perspective": "我不是不让你走，但我想知道房贷和医保怎么办。"},
    "sim:sister": {"stance": "先算成本", "perspective": "先列三个月的支出和能接的活，再决定什么时候说。"},
}


def scenario_create(**changes):
    data = {
        "request_id": REQUEST,
        "title": "和家里说想换工作",
        "situation": "我正在考虑辞职换一份收入不稳定但更喜欢的工作，今晚要和妈妈、姐姐吃饭。",
        "goal": "把想法说清楚，也听听她们的担心，不吵起来。",
        "concern": "怕一开口就变成辩解，也怕她们觉得我不负责任。",
        "participants": PARTICIPANTS,
        "descriptions": DESCRIPTIONS,
    }
    data.update(changes)
    return ScenarioCreate(**data)


def round_request(message="我想先告诉她们我的计划。", **changes):
    data = {"request_id": f"req-round-{uuid4().hex[:12]}", "message": message}
    data.update(changes)
    return ScenarioRoundRequest(**data)


class Model:
    def __init__(self, *, fail=False, duplicate=False, hook=None):
        self.calls = 0
        self.prompts = []
        self.fail = fail
        self.duplicate = duplicate
        self.hook = hook

    def structured_call_for_session(self, prompt, schema, session):
        self.calls += 1
        self.prompts.append(prompt)
        if self.hook:
            self.hook()
        if schema.__name__ == "SuggestionOutput":
            return SimpleNamespace(value=schema.model_validate({"suggestions": [
                {"intent": "澄清", "text": "我想先说说我的计划，你们先别急着反对。"},
                {"intent": "表达感受", "text": "我有点怕一说就被否定，所以想慢慢讲。"},
                {"intent": "提出计划", "text": "我们可以先一起算算最坏的情况。"},
            ]}))
        if self.fail:
            raise RuntimeError("provider unavailable")
        block = prompt.split("# Scenario\n", 1)[1].split("\n# Explicit User Experience Preferences", 1)[0]
        cast = json.loads(block)["participants"]
        perspectives = []
        for index, item in enumerate(cast):
            key = item["participant_key"]
            base = VIEWS.get(key) or {"stance": f"角度{index}", "perspective": f"{key} 自己的看法"}
            if self.duplicate:
                base = {"stance": "同一个答案", "perspective": "大家都应该先冷静下来。"}
            perspectives.append({"participant_id": key, **base})
        replies = []
        for item in cast:
            if item["actor_type"] == "SIMULATED_CHARACTER":
                replies.append({"participant_id": item["participant_key"],
                                "message": f"{item['display_name']}说：我听见了，但你得给我一个具体的安排。"})
        return SimpleNamespace(value=schema.model_validate(
            {"perspectives": perspectives, "replies": replies}))


def build(tmp_path, model=None, name="scenario.db"):
    store = SQLiteStore(tmp_path / name)
    activities = ActivityRepository(store)
    scenarios = ScenarioRepository(store)
    model = model or Model()
    service = RelationshipScenarioService(
        store, activity_repository=activities, scenario_repository=scenarios,
        persona_for=lambda char: f"Persona {char}",
        profile_for=lambda: {"version": 0, "preferences": {}},
        model_for=lambda: model)
    return store, activities, scenarios, service, model


def activate(repo, activity_id):
    return repo.act(activity_id, "RESUME",
                    ActivityAction(request_id="req-start-0001", expected_version=1))["activity"]


def test_scenario_cast_requires_two_to_three_with_both_identities():
    with pytest.raises(ValidationError):
        scenario_create(participants=PARTICIPANTS[:1])
    with pytest.raises(ValidationError):
        scenario_create(participants=PARTICIPANTS + [
            {"actor_type": "CHARACTER", "character_id": "momo"}])
    with pytest.raises(ValidationError):
        scenario_create(participants=[PARTICIPANTS[0],
                                      {"actor_type": "CHARACTER", "character_id": "momo"}])
    with pytest.raises(ValidationError):
        scenario_create(descriptions={"sim:nobody": "不存在的人"})


def test_create_persists_scene_and_never_enters_real_memory(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    result = service.create(scenario_create())
    activity = result["activity"]
    assert activity["status"] == "CREATED" and activity["version"] == 1
    assert result["scenario"]["situation"].startswith("我正在考虑辞职")
    assert result["scenario"]["notes"]["sim:mother"].startswith("担心我辞职")
    assert result["replayed"] is False
    replay = service.create(scenario_create())
    assert replay["replayed"] is True and replay["activity"]["id"] == activity["id"]
    assert model.calls == 0
    assert store.list_memories("rin") == []
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert is_real_experience({"experience_scope": activity_experience_scope(activity["id"])}) is False
    store.close()


def test_practice_produces_distinct_views_allowing_disagreement(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    result = service.practice(activity["id"], round_request())
    assert model.calls == 1
    assert result["replayed"] is False
    perspectives = result["perspectives"]
    assert {item["participant_key"] for item in perspectives} == {"rin", "sim:mother", "sim:sister"}
    assert len({item["stance"] for item in perspectives}) == 3
    # The two simulated partners disagree on what matters, and neither is
    # rewritten into a single shared conclusion.
    assert "先算成本" in {item["stance"] for item in perspectives}
    assert "担心风险" in {item["stance"] for item in perspectives}
    replies = result["replies"]
    assert {item["participant_key"] for item in replies} == {"sim:mother", "sim:sister"}
    for event in result["events"]:
        assert event["experience_scope"] == activity_experience_scope(activity["id"])
        assert is_real_experience({"experience_scope": event["experience_scope"]}) is False
    # The practice writes no real fact.
    assert store.list_memories("rin") == []
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0] == 0
    store.close()


def test_replay_is_idempotent_and_does_not_call_the_model_twice(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    request = round_request()
    first = service.practice(activity["id"], request)
    second = service.practice(activity["id"], request)
    assert second["replayed"] is True and second["events"] == first["events"]
    assert model.calls == 1
    with pytest.raises(ScenarioConflict):
        service.practice(activity["id"], request.model_copy(update={"message": "另一句话"}))
    assert model.calls == 1
    store.close()


def test_failure_is_durable_and_never_fabricates_partner_facts(tmp_path):
    model = Model(fail=True)
    store, activities, scenarios, service, _ = build(tmp_path, model)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    request = round_request()
    with pytest.raises(ScenarioUnavailable):
        service.practice(activity["id"], request)
    assert scenarios.rounds(activity["id"])[0]["status"] == "FAILED"
    with pytest.raises(ScenarioUnavailable):
        service.practice(activity["id"], request)
    assert model.calls == 1
    events = scenarios.events(activity["id"])
    assert [item["event_type"] for item in events] == ["USER_MESSAGE"]
    assert store.list_memories("rin") == []
    store.close()


def test_unified_views_are_rejected_not_published(tmp_path):
    model = Model(duplicate=True)
    store, activities, scenarios, service, _ = build(tmp_path, model)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    with pytest.raises(ScenarioUnavailable):
        service.practice(activity["id"], round_request())
    assert [item["event_type"] for item in scenarios.events(activity["id"])] == ["USER_MESSAGE"]
    assert scenarios.rounds(activity["id"])[0]["status"] == "FAILED"
    store.close()


def test_turn_requires_active_scene(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    with pytest.raises(ScenarioConflict):
        service.practice(activity["id"], round_request())
    assert model.calls == 0
    activate(activities, activity["id"])
    activities.act(activity["id"], "PAUSE",
                   ActivityAction(request_id="req-pause-0001", expected_version=2))
    with pytest.raises(ScenarioConflict):
        service.practice(activity["id"], round_request())
    assert model.calls == 0
    store.close()


def test_state_change_during_generation_is_not_published(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    model.hook = lambda: activities.act(
        activity["id"], "PAUSE",
        ActivityAction(request_id="req-pause-0001", expected_version=2))
    with pytest.raises(ScenarioConflict):
        service.practice(activity["id"], round_request())
    assert [item["event_type"] for item in scenarios.events(activity["id"])] == ["USER_MESSAGE"]
    assert scenarios.rounds(activity["id"])[0]["status"] == "FAILED"
    store.close()


def _suggestion_service(store, scenarios, model):
    return ReplySuggestionService(
        store, ensure_character=lambda char: {"id": char},
        persona_for=lambda char: f"Persona {char}", model_for=lambda: model,
        scenario_for=lambda activity_id, event_id: scenarios.suggestion_snapshot(activity_id, event_id))


def test_drafts_reuse_the_existing_batch_and_read_only_practice_context(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    turn = service.practice(activity["id"], round_request("今晚我想先讲我的计划。"))
    latest = max(item["id"] for item in turn["events"])
    drafts_model = Model()
    drafts = _suggestion_service(store, scenarios, drafts_model)
    request = SuggestionRequest(
        request_id=uuid4(),
        context_ref=FactRef(kind="SCENARIO_EVENT", id=latest, activity_id=activity["id"]),
        expected_profile_version=0)
    batch = drafts.generate(request)
    assert batch["status"] == "READY" and len(batch["suggestions"]) == 3
    prompt = drafts_model.prompts[0]
    assert "SIMULATED" in prompt and "妈妈" in prompt and "先算成本" in prompt
    assert "Persona rin" in prompt
    # A new practice fact invalidates the batch; it does not silently regenerate.
    service.practice(activity["id"], round_request("我还没想好房贷怎么办。"))
    with pytest.raises(SuggestionStale):
        drafts.get(batch["batch_id"])
    assert drafts_model.calls == 1
    store.close()


def test_draft_attribution_classifies_select_edit_and_free_input(tmp_path):
    store, activities, scenarios, service, _ = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    turn = service.practice(activity["id"], round_request("我想先说计划。"))
    latest = max(item["id"] for item in turn["events"])
    drafts_model = Model()
    drafts = _suggestion_service(store, scenarios, drafts_model)
    request = SuggestionRequest(
        request_id=uuid4(),
        context_ref=FactRef(kind="SCENARIO_EVENT", id=latest, activity_id=activity["id"]),
        expected_profile_version=0)
    batch = drafts.generate(request)
    choice = batch["suggestions"][0]

    def signal(source_id, text):
        return record_choice(
            store, (batch, choice),
            FactRef(kind="SCENARIO_EVENT", id=source_id, activity_id=activity["id"]),
            ["SCENARIO", activity["id"]], f"req-send-{uuid4().hex[:10]}")

    # record_choice reads the accepted text back from the practice event; each
    # case uses a fresh user event so the signal identity is unique.
    def new_user_event(text):
        result = service.practice(activity["id"], round_request(text))
        return next(item for item in result["events"] if item["event_type"] == "USER_MESSAGE")["id"]

    assert signal(new_user_event(choice["text"]), choice["text"])["kind"] == "SELECT"
    assert signal(new_user_event("我会自己改一句。"), "我会自己改一句。")["kind"] == "EDIT"
    free = record_choice(
        store, (batch, None),
        FactRef(kind="SCENARIO_EVENT", id=new_user_event("完全自己写。"), activity_id=activity["id"]),
        ["SCENARIO", activity["id"]], f"req-send-{uuid4().hex[:10]}")
    assert free["kind"] == "FREE_INPUT"
    store.close()


def test_real_direct_context_never_contains_practice_material(tmp_path):
    """A real Direct draft must not see the practice transcript or its scope."""
    store, activities, scenarios, service, _ = build(tmp_path)
    activity = service.create(scenario_create())["activity"]
    activate(activities, activity["id"])
    service.practice(activity["id"], round_request("我今晚要和妈妈摊牌。"))
    # A real Direct fact exists in the same database.
    from datetime import datetime, timezone
    from character_memory.domain.models import Event, EventType
    source = store.append_message(Event(
        character_id="rin", event_type=EventType.CHARACTER_MESSAGE,
        event_time=datetime.now(timezone.utc), content="真实私聊里的一句普通问候"))
    real_model = Model()
    real = ReplySuggestionService(
        store, ensure_character=lambda char: {"id": char},
        persona_for=lambda char: f"Persona {char}", model_for=lambda: real_model)
    batch = real.generate(SuggestionRequest(
        request_id=uuid4(), context_ref=FactRef(kind="EVENT", id=source.id),
        expected_profile_version=0))
    assert batch["status"] == "READY"
    prompt = real_model.prompts[0]
    assert "真实私聊里的一句普通问候" in prompt
    assert "摊牌" not in prompt and "sim:mother" not in prompt
    store.close()


def test_fact_ref_serialization_keeps_existing_domains_unchanged():
    real = FactRef(kind="EVENT", id=7)
    group = FactRef(kind="CONVERSATION_EVENT", id=7, conversation_id="group-1")
    practice = FactRef(kind="SCENARIO_EVENT", id=7, activity_id="activity-1")
    # Existing stored/serialized refs must not grow an `activity_id: null`
    # field just because the practice namespace was added.
    assert real.model_dump(mode="json") == {
        "kind": "EVENT", "id": 7, "conversation_id": None}
    assert group.model_dump(mode="json") == {
        "kind": "CONVERSATION_EVENT", "id": 7, "conversation_id": "group-1"}
    assert practice.model_dump(mode="json") == {
        "kind": "SCENARIO_EVENT", "id": 7, "conversation_id": None,
        "activity_id": "activity-1"}
