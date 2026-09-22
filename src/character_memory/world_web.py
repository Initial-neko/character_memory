from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field

from character_memory.browser_web import HeadlessBrowserWebFetcher
from character_memory.world_observation import WorldObservationService


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
    avatar_search = getattr(access, "avatar_search", None)
    search_provider = getattr(avatar_search, "provider", None) if avatar_search is not None else None
    fetcher = HeadlessBrowserWebFetcher(
        timeout_seconds=float(getattr(settings, "web_browser_timeout_seconds", 20.0)),
        render_wait_ms=int(getattr(settings, "web_browser_render_wait_ms", 700)),
        channel=str(getattr(settings, "web_browser_channel", "auto") or "auto"),
    )
    observer = WorldObservationService(search_provider, fetcher)
    access.world_fetcher = fetcher
    access.world_observer = observer

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
