"""Relationship practice: one Activity + distinct partner views + a simulated reply.

The service composes three already-owned pieces and adds no second runtime:

* :class:`ActivityRepository` owns identity, status, version and idempotent
  receipts;
* :class:`ScenarioRepository` owns the scene text and the simulated transcript;
* the existing ``ReplySuggestionService`` generates user drafts, but this module
  hands it practice material through ``ScenarioRepository.suggestion_snapshot``.

There is no separate input path. The user reads partner views, then uses the
same 3-5 editable drafts and free input as ordinary chat; the accepted user
message is written by this repository with the frozen SIMULATED scope, so it can
never be mistaken for a real Direct/Group fact.

Failure is explicit. If the model call fails, the round is durably FAILED and
the response is an error. We never print a plausible partner reply that no model
produced, and a retry with the same request id returns the same failure.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from character_memory.application.reply_suggestions import (
    SuggestionStale,
    SuggestionUnavailable,
)
from character_memory.domain.activity import activity_experience_scope
from character_memory.domain.models import Event, EventType, Memory
from character_memory.domain.reply_suggestions import SuggestionOutput
from character_memory.domain.relationship_scenario import (
    ScenarioAdoptRequest,
    ScenarioBranchCreate,
    ScenarioBranchOutput,
    ScenarioBranchRetry,
    ScenarioCreate,
    ScenarioReviewOutput,
    ScenarioReviewRequest,
    ScenarioRoundOutput,
    ScenarioRoundRequest,
    validate_round_output,
)
from character_memory.llm.usage import llm_usage_scope
from character_memory.storage.activities import (
    ActivityConflict,
    ActivityCorrupt,
    ActivityMissing,
    ActivityRepository,
)
from character_memory.storage.scenarios import (
    ScenarioConflict,
    ScenarioCorrupt,
    ScenarioFailed,
    ScenarioMissing,
    ScenarioRepository,
)


logger = logging.getLogger("character_memory.scenario")


class ScenarioUnavailable(RuntimeError):
    """The model could not produce partner facts; the round is durably FAILED."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BranchUnavailable(RuntimeError):
    """The model could not produce branch facts; the branch is durably FAILED."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ReviewUnavailable(RuntimeError):
    """The review could not be produced; the attempt is durably FAILED."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _participant_views(activity: dict) -> list[dict]:
    """Stable participant keys as the Activity already stores them."""
    views = []
    for item in activity["participants"]:
        if item["actor_type"] == "CHARACTER":
            key = str(item["character_id"])
        else:
            key = f"sim:{item['local_id']}"
        views.append({**item, "participant_key": key})
    return views


class RelationshipScenarioService:
    def __init__(self, store, *, activity_repository=None, scenario_repository=None,
                 persona_for, profile_for, model_for, embed_for=None):
        self.store = store
        self.activities = activity_repository or ActivityRepository(store)
        self.scenarios = scenario_repository or ScenarioRepository(store)
        self.persona_for = persona_for
        self.profile_for = profile_for
        self.model_for = model_for
        # Embedding is best-effort for the one REAL memory an explicit adoption
        # writes. A missing provider must not turn adoption into a fake success:
        # the event still carries the sourced practice summary.
        self.embed_for = embed_for

    # -- creation --------------------------------------------------------

    def create(self, request: ScenarioCreate) -> dict:
        """Create the Activity and its scene atomically.

        Both inserts share one immediate transaction: a scenario that fails to
        persist must not leave a stray Activity, and a replayed request must
        return the original scene instead of inserting a second one.
        """
        with self.store.transaction(immediate=True):
            created = self.activities.create(request.to_activity())
            activity = created["activity"]
            if created["replayed"]:
                scene = self.scenarios.get(activity["id"])
            else:
                scene = self.scenarios.create(request, activity)
            return {"activity": activity, "scenario": scene, "replayed": created["replayed"],
                    "receipt": created["receipt"]}

    # -- reading ---------------------------------------------------------

    def read(self, activity_id: str) -> dict:
        activity = self.activities.get(activity_id)
        if not self.scenarios.exists(activity_id):
            raise ScenarioMissing("SCENARIO_NOT_FOUND")
        return {"activity": activity, "scenario": self.scenarios.get(activity_id),
                "experience_scope": activity_experience_scope(activity_id)}

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        items = []
        for activity in self.activities.list(limit=limit, status=status):
            if self.scenarios.exists(activity["id"]):
                items.append({"activity": activity, "scenario": self.scenarios.get(activity["id"])})
        return items

    # -- one practice turn ----------------------------------------------

    def _cast(self, activity: dict) -> list[dict]:
        return _participant_views(activity)

    def _prompt(self, activity: dict, scene: dict, request: ScenarioRoundRequest) -> str:
        cast = self._cast(activity)
        personas = {}
        for item in cast:
            if item["actor_type"] == "CHARACTER":
                personas[item["participant_key"]] = self.persona_for(item["participant_key"])
        notes = scene["notes"]
        profile = self.profile_for()
        return (
            "你在陪用户做一次现实关系练习。这不是现实已发生的事，所有人都在模拟情境中；"
            "不能把练习内容当成真实经历，也不能替用户决定。\n"
            "每位伙伴必须给出自己的看法，观点必须可辨认、允许彼此分歧；不要统一成一套标准答案，"
            "不要都说教，不要强行达成一致。模拟人物按自己的身份回应用户，行为要合理：可以拒绝、"
            "犹豫、误解、提出条件，也可以只回应一部分。正式人物只提供视角，不代替模拟人物说话。\n"
            "不要编造用户没说过的事实，不要假装练习已经产生现实后果。\n"
            "严格返回一个以 perspectives 和 replies 为字段的对象（perspectives 每位参与者一项，"
            "只有模拟人物可以出现在 replies 中）。\n"
            "Output JSON Schema:\n" + json.dumps(ScenarioRoundOutput.model_json_schema(), ensure_ascii=False)
            + "\n# Scenario\n"
            + json.dumps({
                "activity_id": activity["id"],
                "title": activity["title"],
                "situation": scene["situation"],
                "goal": scene["goal"],
                "concern": scene["concern"],
                "participants": [{**item, "description": notes.get(item["participant_key"])}
                                 for item in cast],
                "transcript": scene["events"],
            }, ensure_ascii=False)
            + "\n# Explicit User Experience Preferences\n"
            + json.dumps(profile, ensure_ascii=False)
            + "\n# User's current turn\n" + request.message
        )

    def practice(self, activity_id: str, request: ScenarioRoundRequest, *,
                 on_user_event=None) -> dict:
        """Run one turn: perspectives from every partner, replies from simulated ones."""
        try:
            claim = self.scenarios.begin_round(activity_id, request, on_user_event=on_user_event)
        except ScenarioFailed as exc:
            raise ScenarioUnavailable(exc.code) from exc
        if not claim["claimed"]:
            return claim
        turn_id = claim["turn_id"]
        try:
            activity = self.activities.get(activity_id)
            scene = self.scenarios.get(activity_id)
            prompt = self._prompt(activity, {"situation": scene["situation"], "goal": scene["goal"],
                                             "concern": scene["concern"], "notes": scene["notes"],
                                             "events": scene["events"][:-1]}, request)
            with llm_usage_scope(feature="RELATIONSHIP_SCENARIO", purpose="SIMULATED_PRACTICE",
                                 conversation_id="scenario:" + activity_id, override=True):
                result = self.model_for().structured_call_for_session(
                    prompt, ScenarioRoundOutput, "scenario:" + activity_id)
            output = ScenarioRoundOutput.model_validate(result.value)
            participants = _participant_views(activity)
            validate_round_output(
                output,
                participant_ids=[item["participant_key"] for item in participants],
                simulated_ids=[item["participant_key"] for item in participants
                               if item["actor_type"] == "SIMULATED_CHARACTER"],
            )
        except ScenarioFailed as exc:
            raise ScenarioUnavailable(exc.code) from exc
        except ScenarioConflict:
            raise
        except (ActivityMissing, ScenarioMissing) as exc:
            raise ScenarioMissing("SCENARIO_NOT_FOUND") from exc
        except ScenarioCorrupt:
            raise
        except ActivityCorrupt:
            raise
        except Exception as exc:
            # Any provider, schema or identity failure is a failed round. The
            # detail stays in the log; the client gets a stable code. Nothing
            # from the failed generation is persisted as partner speech.
            logger.warning("scenario.round_failed activity=%s error=%s", activity_id, type(exc).__name__)
            self.scenarios.fail_round(activity_id, request, "SCENARIO_GENERATION_FAILED")
            raise ScenarioUnavailable("SCENARIO_GENERATION_FAILED") from exc
        try:
            return self.scenarios.commit_round(
                activity_id, request, turn_id, output, participants=participants)
        except ScenarioConflict as exc:
            self.scenarios.fail_round(activity_id, request, "SCENARIO_STATE_CHANGED")
            raise ScenarioConflict(str(exc)) from exc

    # -- branches --------------------------------------------------------

    def _scenario_payload(self, activity: dict, scene: dict, events: list[dict]) -> dict:
        cast = self._cast(activity)
        notes = scene["notes"]
        profile = self.profile_for()
        return {
            "activity_id": activity["id"],
            "title": activity["title"],
            "situation": scene["situation"],
            "goal": scene["goal"],
            "concern": scene["concern"],
            "participants": [{**item, "description": notes.get(item["participant_key"])}
                             for item in cast],
            "transcript": events,
            "_profile": profile,
            "_cast": cast,
        }

    def _branch_prompt(self, activity: dict, scene: dict, events: list[dict],
                       *, after_event_id: int, message: str) -> str:
        payload = self._scenario_payload(activity, scene, events)
        profile = payload.pop("_profile")
        payload.pop("_cast")
        return (
            "这是一次模拟关系练习里的一条想象分支，不是现实已经发生的事。"
            "用户没有沿着原来的对话继续，而是在分叉点说了另一句话。"
            "请据此生成每位伙伴在那句话之后的看法、模拟人物的回应，以及这条分支可能导向的后果（consequence）。"
            "后果必须是这条分支里的模拟走向，不能写成现实已经发生的结果，也不能替用户决定。"
            "每位伙伴的观点必须可辨认、允许分歧，不要统一成标准答案；正式人物只提供视角，"
            "只有模拟人物可以出现在 replies 中。\n"
            "严格返回一个以 perspectives、replies、consequence 为字段的对象。\n"
            "Output JSON Schema:\n" + json.dumps(ScenarioBranchOutput.model_json_schema(), ensure_ascii=False)
            + "\n# Scenario\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n# Branch Point\n"
            + json.dumps({"after_event_id": int(after_event_id)}, ensure_ascii=False)
            + "\n# Alternative User Turn\n" + message
            + "\n# Explicit User Experience Preferences\n"
            + json.dumps(profile, ensure_ascii=False)
        )

    def _review_prompt(self, activity: dict, scene: dict, events: list[dict],
                       branches: list[dict]) -> str:
        payload = self._scenario_payload(activity, scene, events)
        profile = payload.pop("_profile")
        payload.pop("_cast")
        alternatives = []
        for branch in branches:
            if branch["status"] != "READY":
                continue
            branch_events = self.scenarios.branch_events(activity["id"], branch["branch_id"])
            user_event = next((item for item in branch_events
                               if item["event_type"] == "USER_MESSAGE"), None)
            alternatives.append({
                "branch_id": branch["branch_id"],
                "alternative_turn": user_event["content"] if user_event else None,
                "consequence": branch["consequence"],
            })
        return (
            "这是一次已经结束的模拟关系练习的复盘。只根据下面的记录总结，不要编造没有发生过的内容，"
            "也不要把模拟分支写成现实后果。\n"
            "返回一个对象：overview 概括这次练习发生了什么；strategy 概括用户实际尝试的策略；"
            "feedback 概括用户收到的反馈（包括分歧和拒绝，不美化）；next_step 可选，只写一个具体、"
            "可执行且不替用户做决定的下一步。\n"
            "Output JSON Schema:\n" + json.dumps(ScenarioReviewOutput.model_json_schema(), ensure_ascii=False)
            + "\n# Scenario\n"
            + json.dumps({**payload, "branches": alternatives}, ensure_ascii=False)
            + "\n# Explicit User Experience Preferences\n"
            + json.dumps(profile, ensure_ascii=False)
        )

    def create_branch(self, activity_id: str, request: ScenarioBranchCreate) -> dict:
        """Generate one imagined alternative from a main-transcript branch point."""
        try:
            claim = self.scenarios.begin_branch(activity_id, request)
        except ScenarioFailed as exc:
            raise BranchUnavailable(exc.code) from exc
        if not claim["claimed"]:
            return claim
        branch_id = claim["branch_id"]
        try:
            activity = self.activities.get(activity_id)
            scene = self.scenarios.get(activity_id)
            events = [item for item in scene["events"]
                      if item["id"] <= claim["after_event_id"]]
            prompt = self._branch_prompt(
                activity,
                {"situation": scene["situation"], "goal": scene["goal"],
                 "concern": scene["concern"], "notes": scene["notes"]},
                events, after_event_id=claim["after_event_id"], message=claim["message"])
            with llm_usage_scope(feature="RELATIONSHIP_SCENARIO", purpose="SIMULATED_BRANCH",
                                 conversation_id="scenario:" + activity_id, override=True):
                result = self.model_for().structured_call_for_session(
                    prompt, ScenarioBranchOutput, "scenario-branch:" + activity_id)
            output = ScenarioBranchOutput.model_validate(result.value)
            participants = _participant_views(activity)
            validate_round_output(
                output,
                participant_ids=[item["participant_key"] for item in participants],
                simulated_ids=[item["participant_key"] for item in participants
                               if item["actor_type"] == "SIMULATED_CHARACTER"],
            )
        except ScenarioFailed as exc:
            raise BranchUnavailable(exc.code) from exc
        except ScenarioConflict:
            raise
        except (ActivityMissing, ScenarioMissing) as exc:
            raise ScenarioMissing("SCENARIO_NOT_FOUND") from exc
        except (ScenarioCorrupt, ActivityCorrupt):
            raise
        except Exception as exc:
            logger.warning("scenario.branch_failed activity=%s branch=%s error=%s",
                           activity_id, branch_id, type(exc).__name__)
            self.scenarios.fail_branch(activity_id, branch_id, request.request_id,
                                       "SCENARIO_BRANCH_GENERATION_FAILED")
            raise BranchUnavailable("SCENARIO_BRANCH_GENERATION_FAILED") from exc
        try:
            committed = self.scenarios.commit_branch(
                activity_id, branch_id, request.request_id, claim["turn_id"], output,
                participants=participants)
            return {"replayed": False, **committed}
        except ScenarioConflict as exc:
            self.scenarios.fail_branch(activity_id, branch_id, request.request_id,
                                       "SCENARIO_STATE_CHANGED")
            raise ScenarioConflict(str(exc)) from exc

    def retry_branch(self, activity_id: str, branch_id: str,
                     request: ScenarioBranchRetry) -> dict:
        """Retry a failed branch with a new request id; never reuses its failure."""
        try:
            claim = self.scenarios.begin_branch_retry(activity_id, branch_id, request)
        except ScenarioFailed as exc:
            raise BranchUnavailable(exc.code) from exc
        if not claim["claimed"]:
            return claim
        try:
            activity = self.activities.get(activity_id)
            scene = self.scenarios.get(activity_id)
            events = [item for item in scene["events"]
                      if item["id"] <= claim["after_event_id"]]
            prompt = self._branch_prompt(
                activity,
                {"situation": scene["situation"], "goal": scene["goal"],
                 "concern": scene["concern"], "notes": scene["notes"]},
                events, after_event_id=claim["after_event_id"], message=claim["message"])
            with llm_usage_scope(feature="RELATIONSHIP_SCENARIO", purpose="SIMULATED_BRANCH",
                                 conversation_id="scenario:" + activity_id, override=True):
                result = self.model_for().structured_call_for_session(
                    prompt, ScenarioBranchOutput, "scenario-branch:" + activity_id)
            output = ScenarioBranchOutput.model_validate(result.value)
            participants = _participant_views(activity)
            validate_round_output(
                output,
                participant_ids=[item["participant_key"] for item in participants],
                simulated_ids=[item["participant_key"] for item in participants
                               if item["actor_type"] == "SIMULATED_CHARACTER"],
            )
        except ScenarioFailed as exc:
            raise BranchUnavailable(exc.code) from exc
        except ScenarioConflict:
            raise
        except (ActivityMissing, ScenarioMissing) as exc:
            raise ScenarioMissing("SCENARIO_NOT_FOUND") from exc
        except (ScenarioCorrupt, ActivityCorrupt):
            raise
        except Exception as exc:
            logger.warning("scenario.branch_retry_failed activity=%s branch=%s error=%s",
                           activity_id, branch_id, type(exc).__name__)
            self.scenarios.fail_branch(activity_id, branch_id, request.request_id,
                                       "SCENARIO_BRANCH_GENERATION_FAILED")
            raise BranchUnavailable("SCENARIO_BRANCH_GENERATION_FAILED") from exc
        try:
            committed = self.scenarios.commit_branch(
                activity_id, branch_id, request.request_id, claim["turn_id"], output,
                participants=participants)
            return {"replayed": False, **committed}
        except ScenarioConflict as exc:
            self.scenarios.fail_branch(activity_id, branch_id, request.request_id,
                                       "SCENARIO_STATE_CHANGED")
            raise ScenarioConflict(str(exc)) from exc

    def read_branch(self, activity_id: str, branch_id: str) -> dict:
        return self.scenarios.branch(activity_id, branch_id)

    # -- review ----------------------------------------------------------

    def review(self, activity_id: str, request: ScenarioReviewRequest) -> dict:
        """Generate one review of an ENDED scenario; failure is durable and retryable."""
        try:
            claim = self.scenarios.begin_review(activity_id, request)
        except ScenarioFailed as exc:
            raise ReviewUnavailable(exc.code) from exc
        if not claim["claimed"]:
            return claim
        try:
            activity = self.activities.get(activity_id)
            scene = self.scenarios.get(activity_id)
            prompt = self._review_prompt(
                activity,
                {"situation": scene["situation"], "goal": scene["goal"],
                 "concern": scene["concern"], "notes": scene["notes"]},
                scene["events"], scene["branches"])
            with llm_usage_scope(feature="RELATIONSHIP_SCENARIO", purpose="SCENARIO_REVIEW",
                                 conversation_id="scenario:" + activity_id, override=True):
                result = self.model_for().structured_call_for_session(
                    prompt, ScenarioReviewOutput, "scenario-review:" + activity_id)
            output = ScenarioReviewOutput.model_validate(result.value)
        except ScenarioConflict:
            raise
        except (ActivityMissing, ScenarioMissing) as exc:
            raise ScenarioMissing("SCENARIO_NOT_FOUND") from exc
        except (ScenarioCorrupt, ActivityCorrupt):
            raise
        except Exception as exc:
            logger.warning("scenario.review_failed activity=%s error=%s",
                           activity_id, type(exc).__name__)
            self.scenarios.fail_review(activity_id, request.request_id,
                                       "SCENARIO_REVIEW_FAILED")
            raise ReviewUnavailable("SCENARIO_REVIEW_FAILED") from exc
        try:
            committed = self.scenarios.commit_review(activity_id, request.request_id, output)
            return {"replayed": False, **committed}
        except ScenarioConflict as exc:
            self.scenarios.fail_review(activity_id, request.request_id,
                                       "SCENARIO_STATE_CHANGED")
            raise ScenarioConflict(str(exc)) from exc

    def read_review(self, activity_id: str) -> dict:
        if not self.scenarios.exists(activity_id):
            raise ScenarioMissing("SCENARIO_NOT_FOUND")
        return self.scenarios.review(activity_id)

    # -- explicit adoption ----------------------------------------------

    def _adoption_texts(self, activity: dict, review: dict, consequence: str) -> tuple[str, str]:
        title = activity["title"]
        overview = review["overview"]
        strategy = review["strategy"]
        feedback = review["feedback"]
        next_step = review["next_step"]
        next_text = f"下次可以尝试：{next_step}。" if next_step else ""
        event_text = (
            f"我们做了一次模拟关系练习《{title}》。练习内容：{overview} "
            f"你当时尝试的方式：{strategy} 你收到的反馈：{feedback} "
            f"我们设想过的一条分支走向：{consequence} {next_text}"
            "这条记录来自用户显式采用；练习本身发生在模拟情境里，不是现实世界中已经发生的事。"
        )
        memory_text = (
            f"共同练习经历（用户显式采用）：在一次模拟关系练习《{title}》里，我陪你练习过。"
            f"练习内容：{overview} 你尝试的方式：{strategy} 你收到的反馈：{feedback} "
            f"我们设想过的一条分支走向：{consequence} {next_text}"
            "这是模拟练习后被你采用为共同经历，不是现实里已经发生的真实事件。"
        )
        return event_text, memory_text

    def adopt_branch(self, activity_id: str, request: ScenarioAdoptRequest) -> dict:
        """Explicitly turn one READY branch into exactly one REAL shared memory.

        This is the only place in the scenario slice that writes real rows. It
        re-reads the adoption receipt inside the write transaction, so two
        clients cannot create two memories for the same scenario.
        """
        activity = self.activities.get(activity_id)
        if not self.scenarios.exists(activity_id):
            raise ScenarioMissing("SCENARIO_NOT_FOUND")
        existing = self.scenarios.adoption(activity_id)
        if existing is not None:
            if existing["branch_id"] != request.branch_id:
                raise ScenarioConflict("SCENARIO_ALREADY_ADOPTED")
            if request.character_id is not None and request.character_id != existing["character_id"]:
                raise ScenarioConflict("SCENARIO_ALREADY_ADOPTED")
            return {"adoption": existing, "replayed": True,
                    "event_id": existing["event_id"], "memory_id": existing["memory_id"]}
        if activity["status"] != "ENDED":
            raise ScenarioConflict("SCENARIO_NOT_ENDED")
        review_state = self.scenarios.review(activity_id)
        if review_state["status"] != "READY":
            raise ScenarioConflict("SCENARIO_REVIEW_NOT_READY")
        branch = self.scenarios.branch(activity_id, request.branch_id)
        if branch["status"] != "READY":
            raise ScenarioConflict("SCENARIO_BRANCH_NOT_READY")
        formal = [item["character_id"] for item in activity["participants"]
                  if item["actor_type"] == "CHARACTER"]
        character_id = request.character_id or (formal[0] if len(formal) == 1 else None)
        if character_id is None:
            raise ScenarioConflict("SCENARIO_ADOPT_CHARACTER_REQUIRED")
        if character_id not in formal:
            raise ScenarioConflict("SCENARIO_ADOPT_CHARACTER_INVALID")
        review = review_state["review"]
        event_text, memory_text = self._adoption_texts(
            activity, review, str(branch["consequence"]))
        embedding = None
        if self.embed_for is not None:
            try:
                embedding = self.embed_for(memory_text)
            except Exception as exc:  # embedding is auxiliary; never fabricate a success
                logger.warning("scenario.adoption_embedding_failed activity=%s error=%s",
                               activity_id, type(exc).__name__)
        source = {
            "kind": "SCENARIO_ADOPTION",
            "action": "ADOPT_BRANCH",
            "activity_id": activity_id,
            "branch_id": branch["branch_id"],
            "adoption_request_id": request.request_id,
            "review_request_id": review["request_id"],
        }
        practice = {
            "title": activity["title"],
            "overview": review["overview"],
            "strategy": review["strategy"],
            "feedback": review["feedback"],
            "next_step": review["next_step"],
            "consequence": branch["consequence"],
            "branch_after_event_id": branch["after_event_id"],
        }
        now = datetime.now(timezone.utc)
        with self.store.transaction(immediate=True):
            latest = self.scenarios.adoption(activity_id)
            if latest is not None:
                if latest["branch_id"] != request.branch_id:
                    raise ScenarioConflict("SCENARIO_ALREADY_ADOPTED")
                return {"adoption": latest, "replayed": True,
                        "event_id": latest["event_id"], "memory_id": latest["memory_id"]}
            # Re-read the durable gates inside the write lock. ENDED is terminal
            # and READY branch/review rows are immutable, so this can only fail
            # if the caller raced the first adoption.
            if self.activities.get(activity_id)["status"] != "ENDED":
                raise ScenarioConflict("SCENARIO_NOT_ENDED")
            if self.scenarios.review(activity_id)["status"] != "READY":
                raise ScenarioConflict("SCENARIO_REVIEW_NOT_READY")
            if self.scenarios.branch(activity_id, request.branch_id)["status"] != "READY":
                raise ScenarioConflict("SCENARIO_BRANCH_NOT_READY")
            event = self.store.append_event(Event(
                character_id=character_id,
                event_type=EventType.PRACTICE_ADOPTED,
                event_time=now,
                content=event_text,
                metadata={
                    "experience_scope": {"schema_version": 1, "reality": "REAL"},
                    "channel": "PRACTICE",
                    "source": source,
                    "practice": practice,
                },
            ))
            memory = self.store.add_memory(Memory(
                character_id=character_id,
                content=memory_text,
                memory_type="SHARED",
                event_time=now,
                importance=0.7,
                source_event_id=event.id,
                metadata={
                    "experience_scope": {"schema_version": 1, "reality": "REAL"},
                    "source": source,
                    "practice": practice,
                },
                embedding=embedding,
            ))
            adoption = self.scenarios.record_adoption(
                activity_id, request, character_id=character_id,
                event_id=int(event.id), memory_id=int(memory.id))
            return {"adoption": adoption, "replayed": False,
                    "event_id": event.id, "memory_id": memory.id}

    # -- drafts on practice context -------------------------------------

    def draft_context(self, activity_id: str, event_id: int) -> dict:
        """The one visible practice source a draft may be generated from."""
        return self.scenarios.suggestion_snapshot(activity_id, event_id)

    def failure(self, activity_id: str, request_id: str) -> str | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT status,failure_code FROM scenario_rounds WHERE activity_id=? AND request_id=?",
                (activity_id, request_id),
            ).fetchone()
            if row is None or row["status"] != "FAILED":
                return None
            return str(row["failure_code"] or "SCENARIO_GENERATION_FAILED")
