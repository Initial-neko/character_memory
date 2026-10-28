"""HTTP surface for relationship practice: scene creation, transcript and turns.

The Activity lifecycle endpoints own pause/resume/end (P10). This module adds
only the practice-specific create/read/turn surface. Reads never load the model;
a turn is the one endpoint that calls it, and a failed call is reported as an
error instead of fabricating partner speech.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Query

from character_memory.application.relationship_scenario import (
    RelationshipScenarioService,
    ScenarioUnavailable,
)
from character_memory.application.reply_suggestion_signals import prepare_choice, record_choice
from character_memory.application.reply_suggestions import SuggestionStale
from character_memory.config import load_persona, resolve_persona_path
from character_memory.domain.activity import activity_experience_scope
from character_memory.domain.facts import FactRef
from character_memory.domain.relationship_scenario import ScenarioCreate, ScenarioRoundRequest
from character_memory.storage.activities import ActivityConflict, ActivityCorrupt, ActivityMissing
from character_memory.storage.experience_profiles import ExperienceProfileRepository
from character_memory.storage.scenarios import (
    ScenarioConflict,
    ScenarioCorrupt,
    ScenarioFailed,
    ScenarioMissing,
    ScenarioRepository,
)


logger = logging.getLogger("character_memory.scenario")


def _scope(activity_id: str) -> dict:
    return activity_experience_scope(activity_id)


def attach_scenario_routes(app) -> None:
    access = app.state.character_memory
    store = access.read_store

    def persona_for(character_id: str) -> str:
        return load_persona(resolve_persona_path(access.settings, character_id))

    def model_for():
        return access.require_bundle().model

    def profile_for():
        return ExperienceProfileRepository(store).get()

    def service() -> RelationshipScenarioService:
        return RelationshipScenarioService(
            store, persona_for=persona_for, profile_for=profile_for, model_for=model_for)

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (ScenarioMissing, ActivityMissing) as exc:
            code = str(exc) if str(exc).startswith("SCENARIO_") else "SCENARIO_NOT_FOUND"
            raise HTTPException(404, code) from exc
        except ScenarioFailed as exc:
            raise HTTPException(502, exc.code) from exc
        except ScenarioUnavailable as exc:
            raise HTTPException(502, exc.code) from exc
        except SuggestionStale as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ScenarioConflict, ActivityConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ScenarioCorrupt, ActivityCorrupt) as exc:
            logger.warning("scenario.corrupt detail=%s", exc)
            raise HTTPException(503, "SCENARIO_CORRUPT") from exc

    def present(activity: dict, scenario: dict | None = None) -> dict:
        payload = {"activity": activity, "experience_scope": _scope(str(activity["id"]))}
        if scenario is not None:
            payload["scenario"] = scenario
        return payload

    def known_characters() -> set[str]:
        return {str(profile["id"]) for profile in access.character_profiles()}

    @app.post("/v2/scenarios", status_code=201)
    def create_scenario(body: ScenarioCreate):
        formal = [item.character_id for item in body.participants if item.actor_type == "CHARACTER"]
        if not formal:
            raise HTTPException(422, "a scenario requires at least one formal character participant")
        missing = sorted(set(formal) - known_characters())
        if missing:
            raise HTTPException(422, f"unknown formal participant: {', '.join(missing)}")
        result = call(service().create, body)
        return {**present(result["activity"], result["scenario"]),
                "replayed": result["replayed"], "receipt": result["receipt"]}

    @app.get("/v2/scenarios")
    def list_scenarios(
        limit: int = Query(default=50, ge=1, le=100),
        status: str | None = Query(default=None, pattern="^(CREATED|ACTIVE|PAUSED|ENDED)$"),
    ):
        items = call(service().list, limit=limit, status=status)
        return {"scenarios": [present(item["activity"], item["scenario"]) for item in items]}

    @app.get("/v2/scenarios/{activity_id}")
    def read_scenario(activity_id: str):
        result = call(service().read, activity_id)
        return present(result["activity"], result["scenario"])

    @app.post("/v2/scenarios/{activity_id}/messages")
    def practice_turn(activity_id: str, body: ScenarioRoundRequest):
        scenario_service = service()
        prepared = None
        if body.reply_suggestion is not None:
            try:
                prepared = prepare_choice(store, access, body.reply_suggestion,
                                          ["SCENARIO", activity_id])
            except SuggestionStale as exc:
                raise HTTPException(409, str(exc)) from exc

        def bind_signal(user_event_id: int) -> None:
            # Runs inside the accept transaction: if the batch cannot be bound,
            # the user turn is rolled back instead of being silently unattributed.
            if prepared is not None:
                record_choice(
                    store, prepared,
                    FactRef(kind="SCENARIO_EVENT", id=int(user_event_id), activity_id=activity_id),
                    ["SCENARIO", activity_id], body.request_id)

        result = call(scenario_service.practice, activity_id, body, on_user_event=bind_signal)
        activity = call(scenario_service.activities.get, activity_id)
        events = scenario_service.scenarios.events(activity_id)
        return {**result, **present(activity),
                "suggestion_ref": ({"kind": "SCENARIO_EVENT", "id": max(item["id"] for item in events),
                                    "activity_id": activity_id} if events else None)}

    @app.post("/v2/scenarios/{activity_id}/rounds/{request_id}/abandon")
    def abandon_round(activity_id: str, request_id: str):
        """Retire an unresolved round explicitly; never invents partner facts."""
        repo = ScenarioRepository(store)
        if not repo.exists(activity_id):
            raise HTTPException(404, "SCENARIO_NOT_FOUND")
        retired = call(repo.forget_pending, activity_id, request_id)
        return {"retired": retired,
                "scenario": call(repo.get, activity_id),
                "experience_scope": _scope(activity_id)}
