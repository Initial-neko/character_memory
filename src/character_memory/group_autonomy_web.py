from __future__ import annotations

from pydantic import BaseModel, Field

from character_memory.application.group_autonomy import (
    GroupAutonomyScheduler,
    GroupAutonomyService,
)
from character_memory.group_store import GroupRepository


class GroupAutonomyConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: float | None = Field(default=None, ge=10.0, le=10080.0)
    max_messages: int | None = Field(default=None, ge=1, le=4)
    user_quiet_minutes: float | None = Field(default=None, ge=0.0, le=1440.0)
    poll_seconds: float | None = Field(default=None, ge=10.0, le=3600.0)
    rearm: bool = True


def attach_group_autonomy_routes(app) -> None:
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError(
            "create_api() must expose app.state.character_memory before group autonomy routes are attached"
        )

    repository = GroupRepository(access.read_store)
    service = GroupAutonomyService(access, repository)
    scheduler = GroupAutonomyScheduler(
        access,
        repository,
        poll_seconds=float(getattr(access.settings, "group_autonomy_poll_seconds", 60.0)),
    )
    scheduler_capable = bool(getattr(access.settings, "api_key", ""))

    if scheduler_capable:
        @app.on_event("startup")
        def _start_group_autonomy():
            # Keep the thread alive while disabled so Settings/Dev can hot-enable
            # the feature without restarting Character Runtime.
            scheduler.start()

    @app.on_event("shutdown")
    def _stop_group_autonomy():
        scheduler.stop()

    access.group_autonomy_repository = repository
    access.group_autonomy = service
    access.group_autonomy_scheduler = scheduler

    @app.get("/v1/group-autonomy/status")
    def group_autonomy_status():
        return scheduler.status()

    @app.post("/v1/group-autonomy/config")
    def group_autonomy_config(req: GroupAutonomyConfigRequest):
        return scheduler.apply_runtime_config(
            enabled=req.enabled,
            interval_minutes=req.interval_minutes,
            max_messages=req.max_messages,
            user_quiet_minutes=req.user_quiet_minutes,
            poll_seconds=req.poll_seconds,
            rearm=req.rearm,
        )

    @app.post("/v1/group-autonomy/due/{conversation_id}")
    def group_autonomy_due(conversation_id: str):
        try:
            return {
                "conversation_id": conversation_id,
                "state": scheduler.force_due(conversation_id),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/group-autonomy/opportunity/{conversation_id}")
    def group_autonomy_opportunity(conversation_id: str):
        try:
            return service.run_opportunity(
                conversation_id,
                source="DEV",
                respect_user_quiet=False,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"group autonomy failed: {exc}") from exc
