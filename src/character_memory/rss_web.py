from __future__ import annotations

from pydantic import BaseModel, Field


class CreateRssSourceRequest(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=240)
    fetch_interval_minutes: float = Field(default=60.0, ge=10.0, le=10080.0)


class UpdateRssSourceRequest(BaseModel):
    enabled: bool


def attach_rss_routes(app, web_dir, repository, service) -> None:
    from fastapi import HTTPException, Query
    from fastapi.responses import FileResponse

    @app.get("/sources")
    def sources_page():
        return FileResponse(web_dir / "sources.html")

    @app.get("/v1/rss/sources")
    def list_sources():
        return {"sources": repository.list_sources()}

    @app.post("/v1/rss/sources")
    def create_source(payload: CreateRssSourceRequest):
        try:
            source = repository.create_source(
                payload.feed_url,
                name=payload.name,
                interval_minutes=payload.fetch_interval_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # A new subscription should become useful immediately. Failure does not
        # erase the subscription; the user can inspect the error and retry.
        refresh = service.refresh_source(source["id"])
        return {
            "source": repository.get_source(source["id"]),
            "refresh": refresh,
        }

    @app.patch("/v1/rss/sources/{source_id}")
    def update_source(source_id: int, payload: UpdateRssSourceRequest):
        source = repository.set_enabled(source_id, payload.enabled)
        if source is None:
            raise HTTPException(status_code=404, detail="RSS source not found")
        return {"source": source}

    @app.post("/v1/rss/sources/{source_id}/refresh")
    def refresh_source(source_id: int):
        if repository.get_source(source_id) is None:
            raise HTTPException(status_code=404, detail="RSS source not found")
        return service.refresh_source(source_id)

    @app.get("/v1/rss/items")
    def list_items(
        source_id: int | None = Query(default=None),
        limit: int = Query(default=60, ge=1, le=100),
        before_id: int | None = Query(default=None),
    ):
        return {
            "items": repository.list_items(
                source_id=source_id,
                limit=limit,
                before_id=before_id,
            )
        }

    @app.get("/v1/rss/items/{item_id}")
    def get_item(item_id: int):
        item = repository.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="RSS item not found")
        return {"item": item}
