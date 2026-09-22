from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.encounter import EncounterScheduler, EncounterService
from character_memory.encounter_store import EncounterRepository


class EncounterCreateRequest(BaseModel):
    source_type: str = Field(default="AUTO", max_length=16)


class EncounterMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def attach_encounter_routes(app):
    """Attach temporary random encounters without creating formal characters yet."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before encounter routes attach")

    repository = EncounterRepository(access.read_store)
    service = EncounterService(access, repository)
    scheduler = EncounterScheduler(access, repository)

    access.encounter_repository = repository
    access.encounter_service = service
    access.encounter_scheduler = scheduler

    @on_app_event(app, "startup")
    def _start_encounter_scheduler():
        # Keep the worker alive when an API key exists; runtime settings can
        # enable/disable encounters without a restart.
        if getattr(access.settings, "api_key", ""):
            scheduler.start()

    @on_app_event(app, "shutdown")
    def _stop_encounter_scheduler():
        scheduler.stop()

    def active_count() -> int:
        return sum(1 for item in access.character_profiles() if "archived_at" not in item)

    def payload(candidate: dict) -> dict:
        limit = int(getattr(access, "max_active_characters", 20))
        soft_limit = int(getattr(access, "soft_active_characters", 10))
        return {
            **candidate,
            "messages": repository.list_messages(candidate["id"], limit=50),
            "active_character_count": active_count(),
            "active_character_limit": limit,
            "active_character_soft_limit": soft_limit,
            "needs_confirmation": active_count() >= soft_limit and active_count() < limit,
            "can_accept": (
                candidate["status"] not in {"ACCEPTED", "DISMISSED", "EXPIRED", "FAILED"}
                and active_count() < limit
            ),
        }

    @app.get("/v1/encounters")
    def list_encounters(limit: int = 12, include_closed: bool = False):
        candidates = repository.list_candidates(
            limit=max(1, min(int(limit), 50)),
            include_closed=bool(include_closed),
        )
        character_limit = int(getattr(access, "max_active_characters", 20))
        return {
            "encounters": [payload(item) for item in candidates],
            "pending_count": repository.pending_count(),
            "active_character_count": active_count(),
            "active_character_limit": character_limit,
            "active_character_soft_limit": int(getattr(access, "soft_active_characters", 10)),
        }

    @app.get("/v1/encounters/status")
    def encounter_status():
        return {
            **scheduler.status(),
            "active_character_count": active_count(),
            "active_character_limit": int(getattr(access, "max_active_characters", 20)),
            "active_character_soft_limit": int(getattr(access, "soft_active_characters", 10)),
        }

    @app.post("/v1/encounters/dev/opportunity")
    def create_encounter(req: EncounterCreateRequest):
        try:
            result = service.create_candidate(
                now=datetime.now().astimezone(),
                source_type=req.source_type,
            )
            return {**result, "candidate": payload(result["candidate"])}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"邂逅生成失败：{exc}") from exc

    @app.post("/v1/encounters/dev/due")
    def force_encounter_due():
        return {"state": scheduler.force_due(now=datetime.now().astimezone())}

    @app.post("/v1/encounters/{candidate_id}/seen")
    def see_encounter(candidate_id: int):
        try:
            return {"candidate": payload(service.mark_seen(candidate_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/encounters/{candidate_id}/messages")
    def chat_encounter(candidate_id: int, req: EncounterMessageRequest):
        try:
            result = service.chat(candidate_id, req.message)
            return {
                **result,
                "candidate": payload(result["candidate"]),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"临时邂逅回复失败：{exc}") from exc

    @app.post("/v1/encounters/{candidate_id}/accept")
    def accept_encounter(candidate_id: int, confirm_over_soft_limit: bool = False):
        try:
            accepted = service.accept(
                candidate_id,
                confirm_over_soft_limit=confirm_over_soft_limit,
            )
            return {"candidate": payload(accepted)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            status = 409 if ("最多保留" in str(exc) or "closed" in str(exc)) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"留下角色失败：{exc}") from exc

    @app.post("/v1/encounters/{candidate_id}/dismiss")
    def dismiss_encounter(candidate_id: int):
        try:
            return {"candidate": payload(service.dismiss(candidate_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
