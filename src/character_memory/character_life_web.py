from __future__ import annotations

from pathlib import Path
from character_memory.life_conversation import LifeReference, resolve_life_reference
from fastapi import HTTPException, Query
from fastapi.responses import FileResponse

from character_memory.character_life import CharacterLifeReader, CharacterExecutionReader


def attach_character_life_routes(app, access):
    reader = CharacterLifeReader(access.read_store)
    db_path = getattr(getattr(access, "settings", None), "db_path", ":memory:")
    executions = CharacterExecutionReader(access.read_store, Path(db_path).resolve().parent / "claude-executions")

    @app.get("/v1/characters/{character_id}/executions")
    def execution_list(character_id: str, cursor: str = Query("", max_length=1000), limit: int = Query(30, ge=1, le=50), day: str | None = None):
        access.ensure_character(character_id)
        try:
            return executions.list(character_id, cursor=cursor, limit=limit, day=day)
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from error

    @app.get("/v1/characters/{character_id}/executions/{request_id:path}")
    def execution_detail(character_id: str, request_id: str):
        access.ensure_character(character_id)
        item = executions.detail(character_id, request_id)
        if item is None:
            raise HTTPException(404, detail="execution not found")
        return item

    @app.get("/v1/characters/{character_id}/life/conversation")
    def life_conversation(character_id: str, key: str = Query(..., min_length=1, max_length=150)):
        profile = access.ensure_character(character_id)
        if profile and profile.get("archived_at"):
            raise HTTPException(409, "人物已归档，请先恢复")
        return resolve_life_reference(access.read_store, LifeReference(character_id=character_id, key=key))

    @app.get("/life")
    def life_page():
        return FileResponse(access.web_dir / "life.html")

    @app.get("/v1/characters/{character_id}/life")
    def character_life(character_id: str, day: str | None = None,
                       offset: int = Query(480, ge=-840, le=840),
                       channel: str = Query("ALL", pattern="^(ALL|DIRECT|GROUP|SPACE|WORLD|INTENT)$"),
                       view: str = Query("life", pattern="^(life|changes|relationships|intents)$"),
                       query: str = Query("", max_length=200), peer: str = Query("", max_length=100), cursor: str = Query("", max_length=1000),
                       limit: int = Query(30, ge=1, le=50)):
        access.ensure_character(character_id)
        try:
            return reader.timeline(character_id, day=day, offset=offset, channel=channel,
                                   view=view, query=query, peer=peer, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
