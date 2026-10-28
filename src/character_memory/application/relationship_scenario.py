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

from character_memory.application.reply_suggestions import (
    SuggestionStale,
    SuggestionUnavailable,
)
from character_memory.domain.activity import activity_experience_scope
from character_memory.domain.reply_suggestions import SuggestionOutput
from character_memory.domain.relationship_scenario import (
    ScenarioCreate,
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
                 persona_for, profile_for, model_for):
        self.store = store
        self.activities = activity_repository or ActivityRepository(store)
        self.scenarios = scenario_repository or ScenarioRepository(store)
        self.persona_for = persona_for
        self.profile_for = profile_for
        self.model_for = model_for

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
