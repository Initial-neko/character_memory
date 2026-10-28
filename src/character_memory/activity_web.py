"""Activity lifecycle HTTP surface: create/read/pause/resume/end.

These routes never load the model bundle. Reading or resuming an activity after a
restart therefore costs zero model calls; the runtime bundle only warms up on an
unrelated request. Execution, branching and reflection are P12 work and do not
exist here.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Query

from character_memory.domain.activity import (
    ActivityAction,
    ActivityCreate,
    activity_experience_scope,
)
from character_memory.storage.activities import (
    ActivityConflict,
    ActivityCorrupt,
    ActivityMissing,
    ActivityRepository,
)


logger = logging.getLogger("character_memory.activity")


def _present(activity: dict) -> dict:
    """Attach the exact scope every fact produced by this activity must carry."""
    payload = dict(activity)
    payload["experience_scope"] = activity_experience_scope(str(activity["id"]))
    return payload


def attach_activity_routes(app) -> None:
    access = app.state.character_memory
    repository = ActivityRepository(access.read_store)

    def known_characters() -> set[str]:
        return {str(profile["id"]) for profile in access.character_profiles()}

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ActivityMissing as exc:
            raise HTTPException(404, str(exc)) from exc
        except ActivityConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ActivityCorrupt as exc:
            # Stored data no longer matches the frozen contract. The stable code
            # is the client contract; the internal detail goes to the log only.
            logger.warning("activity.corrupt detail=%s", exc)
            raise HTTPException(503, "ACTIVITY_CORRUPT") from exc

    def respond(result: dict) -> dict:
        return {**result, "activity": _present(result["activity"])}

    def dsh_display_names(subject_ids: set[str]) -> dict[str, str]:
        """Resolve registered DSH subjects; the caller never supplies the name.

        Only a registered, non-revoked DSH subject may be written into an
        Activity's participant list. Importing lazily keeps the DSH migration
        out of deployments that never use an external subject.
        """
        from character_memory.dsh.store import DshStore

        subjects = DshStore(access.read_store)
        names: dict[str, str] = {}
        for subject_id in subject_ids:
            subject = subjects.get_subject(subject_id)
            if subject is None or subject["status"] != "ACTIVE":
                raise HTTPException(422, "DSH_PARTICIPANT_MISSING")
            names[subject_id] = str(subject["display_name"])
        return names

    @app.post("/v2/activities", status_code=201)
    def create_activity(body: ActivityCreate):
        formal = [item.character_id for item in body.participants if item.actor_type == "CHARACTER"]
        missing = sorted(set(formal) - known_characters())
        if missing:
            raise HTTPException(422, f"unknown formal participant: {', '.join(missing)}")
        if not formal:
            # A purely simulated or DSH cast has no formal Person to attach
            # later results to. Requiring one keeps the boundary explicit.
            raise HTTPException(422, "an activity requires at least one formal character participant")
        dsh_ids = {item.subject_id for item in body.participants if item.actor_type == "DSH"}
        names = dsh_display_names(dsh_ids) if dsh_ids else None
        return respond(call(repository.create, body, dsh_display_names=names))

    @app.get("/v2/activities")
    def list_activities(
        limit: int = Query(default=50, ge=1, le=100),
        status: str | None = Query(default=None, pattern="^(CREATED|ACTIVE|PAUSED|ENDED)$"),
    ):
        return {"activities": [_present(item)
                               for item in call(repository.list, limit=limit, status=status)]}

    @app.get("/v2/activities/{activity_id}")
    def read_activity(activity_id: str):
        return _present(call(repository.get, activity_id))

    def lifecycle(activity_id: str, operation: str, body: ActivityAction):
        return respond(call(repository.act, activity_id, operation, body))

    @app.post("/v2/activities/{activity_id}/pause")
    def pause_activity(activity_id: str, body: ActivityAction):
        return lifecycle(activity_id, "PAUSE", body)

    @app.post("/v2/activities/{activity_id}/resume")
    def resume_activity(activity_id: str, body: ActivityAction):
        return lifecycle(activity_id, "RESUME", body)

    @app.post("/v2/activities/{activity_id}/end")
    def end_activity(activity_id: str, body: ActivityAction):
        return lifecycle(activity_id, "END", body)
