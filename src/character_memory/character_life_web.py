from __future__ import annotations

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse

from character_memory.character_life import CharacterLifeReader


def attach_character_life_routes(app, access):
    reader = CharacterLifeReader(access.read_store)

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
