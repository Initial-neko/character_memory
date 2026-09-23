from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field

from character_memory.web_lifecycle import on_app_event
from character_memory.world_activity import WorldActivityScheduler, WorldPulseRepository



class WorldFetchRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=6000, ge=500, le=16000)


class WorldSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=240)
    max_pages: int = Field(default=2, ge=1, le=4)
    max_chars_per_page: int = Field(default=6000, ge=500, le=16000)


def attach_world_routes(app) -> None:
    """Attach the public-web observation boundary used by autonomous Space."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before world routes are attached")

    settings = access.settings
    services = access.services
    fetcher = services.world_fetcher
    observer = services.world_observer

    pulse_repository = WorldPulseRepository(access.read_store)
    activity_scheduler = WorldActivityScheduler(
        access,
        pulse_repository,
        poll_seconds=float(getattr(settings, "world_activity_poll_seconds", 60.0)),
    )
    access.world_activity_scheduler = activity_scheduler

    @app.get("/v1/world/status")
    def world_status():
        return {
            "enabled": bool(getattr(settings, "space_world_observation_enabled", True)),
            "search_provider": str(getattr(settings, "search_provider", "") or ""),
            "search_configured": bool(getattr(settings, "search_api_key", "")),
            "browser": "playwright-chromium",
            "browser_channel": str(getattr(settings, "web_browser_channel", "auto") or "auto"),
            "timeout_seconds": float(getattr(settings, "web_browser_timeout_seconds", 20.0)),
            "render_wait_ms": int(getattr(settings, "web_browser_render_wait_ms", 700)),
            "max_pages": int(getattr(settings, "space_world_max_pages", 2)),
            "max_chars_per_page": int(getattr(settings, "space_world_max_chars_per_page", 6000)),
            "activity_enabled": bool(getattr(settings, "world_activity_enabled", True)),
            "pulse_enabled": bool(getattr(settings, "world_pulse_enabled", True)),
            "browse_enabled": bool(getattr(settings, "world_browse_enabled", True)),
            "pulse_sources": list(getattr(settings, "world_pulse_sources", [])),
        }

    @app.post("/v1/world/dev/fetch")
    def world_dev_fetch(req: WorldFetchRequest):
        try:
            page = fetcher.fetch(req.url.strip(), max_chars=req.max_chars)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"page": asdict(page)}

    @app.post("/v1/world/dev/search")
    def world_dev_search(req: WorldSearchRequest):
        try:
            result = observer.observe(
                req.query,
                max_pages=req.max_pages,
                max_chars_per_page=req.max_chars_per_page,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            **result,
            "observations": [
                item.model_dump(mode="json") for item in result["observations"]
            ],
        }


    @app.get("/v1/world/pulse")
    def world_pulse(limit: int = 20):
        return {
            "topics": pulse_repository.list_topics(limit=max(1, min(limit, 100))),
            "sources": list(getattr(settings, "world_pulse_sources", [])),
        }

    @app.get("/v1/world/activity/status")
    def world_activity_status():
        return activity_scheduler.status()

    @app.post("/v1/world/pulse/dev/refresh")
    def world_pulse_refresh():
        try:
            return activity_scheduler.service.refresh_pulse()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/pulse/{topic_id}/dev/discuss")
    def world_pulse_discuss(topic_id: int):
        try:
            return activity_scheduler.service.discuss_topic(topic_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/dev/browse/{character_id}")
    def world_personal_browse(character_id: str):
        try:
            return activity_scheduler.service.browse_character(character_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/activity/dev/run")
    def world_activity_run_once():
        try:
            return {"outcomes": activity_scheduler.run_once()}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/world/activity/dev/due/{kind}/{subject_id}")
    def world_activity_force_due(kind: str, subject_id: str):
        normalized = str(kind or "").strip().upper()
        if normalized not in {"PULSE", "DISCUSS", "BROWSE"}:
            raise HTTPException(status_code=400, detail="kind must be PULSE, DISCUSS or BROWSE")
        if normalized != "BROWSE":
            subject_id = "global"
        activity_scheduler.force_due(normalized, subject_id)
        return {"ok": True, "kind": normalized, "subject_id": subject_id}

    @on_app_event(app, "startup")
    def _start_world_activity():
        activity_scheduler.start()
