"""P12: branches, durable review and explicit adoption into shared memory."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from character_memory.application.relationship_scenario import (
    BranchUnavailable,
    RelationshipScenarioService,
    ReviewUnavailable,
)
from character_memory.domain.activity import ActivityAction
from character_memory.domain.experience_scope import is_real_experience
from character_memory.domain.relationship_scenario import (
    ScenarioAdoptRequest,
    ScenarioBranchCreate,
    ScenarioBranchRetry,
    ScenarioCreate,
    ScenarioReviewRequest,
    ScenarioRoundRequest,
)
from character_memory.storage.activities import ActivityConflict, ActivityRepository
from character_memory.storage.scenarios import ScenarioConflict, ScenarioRepository
from character_memory.storage.sqlite import SQLiteStore


PARTICIPANTS = [
    {"actor_type": "CHARACTER", "character_id": "rin"},
    {"actor_type": "SIMULATED_CHARACTER", "local_id": "mother", "display_name": "妈妈"},
]
DESCRIPTIONS = {"sim:mother": "担心风险，习惯先问最坏的情况"}


def create_body(request_id="req-scenario-branch-1"):
    return ScenarioCreate(
        request_id=request_id,
        title="和妈妈谈换工作",
        situation="我想换一份收入不稳定但更喜欢的工作，今晚和妈妈吃饭。",
        goal="把计划说清楚，也听见她的担心。",
        concern="怕一开口就变成辩解。",
        participants=PARTICIPANTS,
        descriptions=DESCRIPTIONS,
    )


def round_request(message="我想先讲我的计划。"):
    return ScenarioRoundRequest(request_id=f"req-round-{uuid4().hex[:12]}", message=message)


class Model:
    def __init__(self):
        self.calls: list[str] = []
        self.branch_messages: list[str] = []
        self.fail_branch = False
        self.fail_review = False

    def structured_call_for_session(self, prompt, schema, session):
        self.calls.append(schema.__name__)
        if schema.__name__ == "ScenarioBranchOutput":
            alternative = prompt.rsplit("# Alternative User Turn\n", 1)[-1].split(
                "\n# Explicit User Experience Preferences", 1)[0]
            self.branch_messages.append(alternative)
            if self.fail_branch:
                raise RuntimeError("branch provider failed")
            block = prompt.split("# Scenario\n", 1)[1].split("\n# Branch Point", 1)[0]
            cast = json.loads(block)["participants"]
            return SimpleNamespace(value=schema.model_validate({
                "perspectives": [
                    {"participant_id": item["participant_key"],
                     "stance": f"分支立场{index}",
                     "perspective": f"{item['participant_key']} 在分支里的看法 {index}"}
                    for index, item in enumerate(cast)],
                "replies": [
                    {"participant_id": item["participant_key"],
                     "message": f"{item['display_name']}在分支里回应。"}
                    for item in cast if item["actor_type"] == "SIMULATED_CHARACTER"],
                "consequence": f"如果这样说，{alternative[:20]} 会让对话走向另一种结果。",
            }))
        if schema.__name__ == "ScenarioReviewOutput":
            if self.fail_review or "FAIL_REVIEW" in prompt:
                raise RuntimeError("review provider failed")
            return SimpleNamespace(value=schema.model_validate({
                "overview": "你先把计划说清楚，再听见了不同的担心。",
                "strategy": "先说明自己的计划，再逐个回应担心。",
                "feedback": "妈妈担心收入风险，要求具体安排。",
                "next_step": "下次先算清三个月的支出。",
            }))
        block = prompt.split("# Scenario\n", 1)[1].split(
            "\n# Explicit User Experience Preferences", 1)[0]
        cast = json.loads(block)["participants"]
        return SimpleNamespace(value=schema.model_validate({
            "perspectives": [
                {"participant_id": item["participant_key"], "stance": f"立场{index}",
                 "perspective": f"{item['participant_key']} 自己的看法 {index}"}
                for index, item in enumerate(cast)],
            "replies": [
                {"participant_id": item["participant_key"],
                 "message": f"{item['display_name']}说：我听见了。"}
                for item in cast if item["actor_type"] == "SIMULATED_CHARACTER"],
        }))


def build(tmp_path):
    store = SQLiteStore(tmp_path / "branch.db")
    activities = ActivityRepository(store)
    scenarios = ScenarioRepository(store)
    model = Model()
    service = RelationshipScenarioService(
        store, activity_repository=activities, scenario_repository=scenarios,
        persona_for=lambda character_id: f"Persona {character_id}",
        profile_for=lambda: {"version": 0, "preferences": {}},
        model_for=lambda: model,
        embed_for=lambda text: [0.125] * 8)
    return store, activities, scenarios, service, model


def start(service, activities):
    activity = service.create(create_body())["activity"]
    activities.act(activity["id"], "RESUME",
                   ActivityAction(request_id="req-start-0001", expected_version=1))
    return activity["id"]


def end(activities, activity_id):
    current = activities.get(activity_id)
    return activities.act(activity_id, "END", ActivityAction(
        request_id=f"req-end-{uuid4().hex[:10]}",
        expected_version=current["version"]))["activity"]


def first_turn(service, activity_id):
    result = service.practice(activity_id, round_request())
    return result["events"][-1]["id"]


def branch(service, activity_id, after_event_id, message, request_id=None):
    return service.create_branch(activity_id, ScenarioBranchCreate(
        request_id=request_id or f"req-branch-{uuid4().hex[:10]}",
        after_event_id=after_event_id,
        message=message))


def full_flow(tmp_path, branch_messages=("如果我说不出口呢？", "如果我直接拒绝呢？")):
    store, activities, scenarios, service, model = build(tmp_path)
    activity_id = start(service, activities)
    point = first_turn(service, activity_id)
    branches = [branch(service, activity_id, point, message) for message in branch_messages]
    end(activities, activity_id)
    review = service.review(activity_id, ScenarioReviewRequest(
        request_id="req-review-0001"))
    return store, activities, scenarios, service, model, activity_id, branches, review


def test_branches_are_isolated_from_each_other_and_the_main_line(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity_id = start(service, activities)
    point = first_turn(service, activity_id)
    main_before = scenarios.events(activity_id)
    left = branch(service, activity_id, point, "如果我说不出口呢？")
    right = branch(service, activity_id, point, "如果我直接拒绝呢？")

    # Main transcript is unchanged; each branch reads only its own turn.
    assert scenarios.events(activity_id) == main_before
    left_events = scenarios.branch_events(activity_id, left["branch_id"])
    right_events = scenarios.branch_events(activity_id, right["branch_id"])
    assert {item["turn_id"] for item in left_events} == {left["turn_id"]}
    assert {item["turn_id"] for item in right_events} == {right["turn_id"]}
    assert left["turn_id"] != right["turn_id"]
    assert {item["content"] for item in left_events if item["event_type"] == "USER_MESSAGE"} == {
        "如果我说不出口呢？"}
    assert {item["content"] for item in right_events if item["event_type"] == "USER_MESSAGE"} == {
        "如果我直接拒绝呢？"}
    for item in left_events:
        assert item["experience_scope"]["branch_id"] == left["branch_id"]
        assert item["branch_id"] == left["branch_id"]
    for item in right_events:
        assert item["experience_scope"]["branch_id"] == right["branch_id"]
    # Branch facts stay simulated and out of the main suggestion snapshot.
    assert all(not is_real_experience({"experience_scope": item["experience_scope"]})
               for item in left_events + right_events)
    with pytest.raises(ScenarioConflict):
        scenarios.suggestion_snapshot(activity_id, left_events[0]["id"])
    snapshot = scenarios.suggestion_snapshot(activity_id, main_before[-1]["id"])
    assert all(item["branch_id"] is None for item in snapshot["events"])
    # No real fact exists yet.
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.list_memories("rin") == []
    store.close()


def test_branch_failure_is_durable_and_retry_uses_a_new_request(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity_id = start(service, activities)
    point = first_turn(service, activity_id)
    request = ScenarioBranchCreate(
        request_id="req-branch-fail", after_event_id=point,
        message="这句分支会失败")
    model.fail_branch = True
    with pytest.raises(BranchUnavailable) as failure:
        service.create_branch(activity_id, request)
    assert failure.value.code == "SCENARIO_BRANCH_GENERATION_FAILED"
    failed = scenarios.branches(activity_id)[0]
    assert failed["status"] == "FAILED"
    branch_events = scenarios.branch_events(activity_id, failed["branch_id"])
    assert [item["event_type"] for item in branch_events] == ["USER_MESSAGE"]
    calls = len(model.calls)
    with pytest.raises(BranchUnavailable):
        service.create_branch(activity_id, request)
    assert len(model.calls) == calls, "a failed branch is never retried automatically"

    model.fail_branch = False
    retried = service.retry_branch(activity_id, failed["branch_id"], ScenarioBranchRetry(
        request_id="req-branch-retry-1"))
    assert retried["status"] == "READY"
    assert retried["consequence"]
    assert len(scenarios.branch_events(activity_id, failed["branch_id"])) == 1 + 2 + 1
    assert all(item["branch_id"] == failed["branch_id"]
               for item in scenarios.branch_events(activity_id, failed["branch_id"]))
    store.close()


def test_review_requires_end_and_recovers_without_duplicate_writes(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity_id = start(service, activities)
    first_turn(service, activity_id)
    with pytest.raises(ScenarioConflict) as not_ended:
        service.review(activity_id, ScenarioReviewRequest(request_id="req-review-early"))
    assert str(not_ended.value) == "SCENARIO_NOT_ENDED"
    assert scenarios.review(activity_id)["status"] == "NONE"
    end(activities, activity_id)

    model.fail_review = True
    with pytest.raises(ReviewUnavailable) as failed:
        service.review(activity_id, ScenarioReviewRequest(request_id="req-review-fail"))
    assert failed.value.code == "SCENARIO_REVIEW_FAILED"
    state = scenarios.review(activity_id)
    assert state["status"] == "FAILED" and state["review"]["overview"] is None
    calls = len(model.calls)
    with pytest.raises(ReviewUnavailable):
        service.review(activity_id, ScenarioReviewRequest(request_id="req-review-fail"))
    assert len(model.calls) == calls

    model.fail_review = False
    ready = service.review(activity_id, ScenarioReviewRequest(request_id="req-review-retry"))
    assert ready["status"] == "READY"
    assert ready["review"]["strategy"].startswith("先说明")
    calls = len(model.calls)
    replay = service.review(activity_id, ScenarioReviewRequest(request_id="req-review-another"))
    assert replay["status"] == "READY" and replay["replayed"] is True
    assert len(model.calls) == calls, "a READY review is read, not regenerated"
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM scenario_reviews WHERE activity_id=?", (activity_id,)
    ).fetchone()[0]
    assert rows == 1
    store.close()


def test_explicit_adoption_writes_one_real_shared_memory_only(tmp_path):
    store, activities, scenarios, service, model, activity_id, branches, review = full_flow(
        tmp_path)
    assert review["status"] == "READY"
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.list_memories("rin") == []

    adopted = service.adopt_branch(activity_id, ScenarioAdoptRequest(
        request_id="req-adopt-0001", branch_id=branches[0]["branch_id"]))
    assert adopted["replayed"] is False
    events = store.list_events("rin", limit=10)
    memories = store.list_memories("rin")
    assert len(events) == 1 and len(memories) == 1
    event, memory = events[0], memories[0]
    assert is_real_experience(event.metadata)
    assert is_real_experience(memory.metadata)
    assert event.event_type.value == "PRACTICE_ADOPTED"
    assert event.metadata["source"]["action"] == "ADOPT_BRANCH"
    assert event.metadata["source"]["activity_id"] == activity_id
    assert event.metadata["source"]["adoption_request_id"] == "req-adopt-0001"
    assert memory.memory_type == "SHARED" and memory.source_event_id == event.id
    assert memory.embedding and len(memory.embedding) == 8
    for text in (event.content, memory.content):
        assert "先说明自己的计划" in text
        assert "妈妈担心收入风险" in text
        assert "模拟" in text and "不是现实" in text
    # The unadopted branch's own wording never becomes a real fact.
    assert all("如果我直接拒绝呢？" not in item.content for item in events)
    assert all("如果我直接拒绝呢？" not in item.content for item in memories)

    replay = service.adopt_branch(activity_id, ScenarioAdoptRequest(
        request_id="req-adopt-0002", branch_id=branches[0]["branch_id"]))
    assert replay["replayed"] is True and replay["event_id"] == event.id
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert len(store.list_memories("rin")) == 1
    with pytest.raises(ScenarioConflict) as other:
        service.adopt_branch(activity_id, ScenarioAdoptRequest(
            request_id="req-adopt-0003", branch_id=branches[1]["branch_id"]))
    assert str(other.value) == "SCENARIO_ALREADY_ADOPTED"
    store.close()


def test_adoption_requires_ended_scenario_and_ready_review(tmp_path):
    store, activities, scenarios, service, model = build(tmp_path)
    activity_id = start(service, activities)
    point = first_turn(service, activity_id)
    ready_branch = branch(service, activity_id, point, "如果我先停一下呢？")
    with pytest.raises(ScenarioConflict) as active:
        service.adopt_branch(activity_id, ScenarioAdoptRequest(
            request_id="req-adopt-early", branch_id=ready_branch["branch_id"]))
    assert str(active.value) == "SCENARIO_NOT_ENDED"
    end(activities, activity_id)
    with pytest.raises(ScenarioConflict) as no_review:
        service.adopt_branch(activity_id, ScenarioAdoptRequest(
            request_id="req-adopt-no-review", branch_id=ready_branch["branch_id"]))
    assert str(no_review.value) == "SCENARIO_REVIEW_NOT_READY"
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.list_memories("rin") == []
    store.close()
