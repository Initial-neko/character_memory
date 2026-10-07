from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal

from character_memory.rss_sources import RSS_CATEGORIES, rss_today


class CreateRssSourceRequest(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=240)
    fetch_interval_minutes: float | None = Field(default=None, ge=10.0, le=10080.0)


class UpdateRssSourceRequest(BaseModel):
    enabled: bool


def attach_rss_routes(app, web_dir, repository, service, *, default_interval_minutes: float = 60.0) -> None:
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
                interval_minutes=payload.fetch_interval_minutes or default_interval_minutes,
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

    @app.get("/v1/rss/categories")
    def list_categories():
        return {
            "categories": RSS_CATEGORIES,
            # Not plain substring: a Latin keyword only matches on a word
            # boundary, so `ai` no longer hits "email" and `API` no longer hits
            # "rapid". Chinese keywords still match as substrings.
            "match": "title_matches_any_keyword",
        }

    @app.get("/v1/rss/items", summary="按发布时间、标题关键词和类型查询 RSS 文章")
    def list_items(
        source_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=60, ge=1, le=100),
        before_id: int | None = Query(default=None, ge=1, description="上一页最后一篇文章的 ID，按时间和 ID 联合排序"),
        period: Literal["all", "today"] = Query(default="all", description="today 按 UTC+08:00 当天发布时间筛选，未知发布时间不纳入"),
        q: str = Query(default="", max_length=200, description="标题字面包含匹配，忽略英文大小写；不搜索正文"),
        category: str | None = Query(default=None, description="/v1/rss/categories 提供的类型 ID，按标题关键词匹配"),
    ):
        start = rss_today()
        try:
            items = repository.list_items(
                source_id=source_id, limit=limit + 1, before_id=before_id,
                period=period, q=q, category=category, now=start,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        has_more = len(items) > limit
        page = items[:limit]
        return {
            "items": page,
            "has_more": has_more,
            "next_before_id": page[-1]["id"] if has_more else None,
            "query": {
                "period": period, "q": q.strip(), "category": category,
                "source_id": source_id, "timezone": "UTC+08:00",
                "date": start.date().isoformat() if period == "today" else None,
            },
        }

    @app.get("/v1/rss/items/{item_id}")
    def get_item(item_id: int):
        item = repository.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="RSS item not found")
        return {"item": item}
