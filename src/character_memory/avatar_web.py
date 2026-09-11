from __future__ import annotations

from pydantic import BaseModel, Field

from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.config import resolve_avatar_dir
from character_memory.search import BraveSearchProvider


class AvatarSearchRequest(BaseModel):
    query: str = Field(default="", max_length=400)
    limit: int = Field(default=12, ge=1, le=20)


class AvatarSelectRequest(BaseModel):
    search_id: str = Field(min_length=8, max_length=80)
    candidate_id: str = Field(min_length=4, max_length=80)


def attach_avatar_routes(app) -> None:
    """Attach avatar discovery without giving the chat runtime general web tools."""

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before avatar routes are attached")

    settings = access.settings
    avatar_store = AvatarStore(
        resolve_avatar_dir(settings),
        max_bytes=int(getattr(settings, "avatar_max_bytes", 8 * 1024 * 1024)),
    )
    provider_name = str(getattr(settings, "search_provider", "brave") or "brave").strip().lower()
    provider = None
    if provider_name == "brave":
        provider = BraveSearchProvider(
            getattr(settings, "search_api_key", ""),
            country=getattr(settings, "search_country", "ALL"),
            language=getattr(settings, "search_language", "zh"),
            safe_search=getattr(settings, "search_safe_search", "strict"),
        )
    avatar_search = AvatarSearchService(provider, avatar_store)
    app.state.character_memory.avatar_store = avatar_store
    app.state.character_memory.avatar_search = avatar_search

    def profile(character_id: str) -> dict[str, str]:
        for item in access.character_profiles():
            if item["id"] == character_id:
                return item
        raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")

    def avatar_url(character_id: str) -> str:
        version = avatar_store.version(character_id)
        return f"/v1/characters/{character_id}/avatar/asset?v={version}" if version else ""

    def public_profile(item: dict[str, str]) -> dict[str, str]:
        result = {key: value for key, value in item.items() if key != "persona_path"}
        result["avatar_url"] = avatar_url(item["id"])
        return result

    def default_query(item: dict[str, str]) -> str:
        name = str(item.get("name") or item["id"]).strip()
        identity = str(item.get("identity") or "").strip()
        # Identity helps distinguish a known fictional character without dumping
        # the full Persona/personality into a public search query.
        parts = [name]
        if identity and identity.casefold() not in name.casefold():
            parts.append(identity[:80])
        parts.extend(["portrait", "avatar", "profile picture"])
        return " ".join(part for part in parts if part).strip()[:400]

    @app.get("/v1/character-profiles")
    def character_profiles_with_avatars():
        return {"characters": [public_profile(item) for item in access.character_profiles()]}

    @app.get("/v1/characters/{character_id}/avatar")
    def get_avatar(character_id: str):
        profile(character_id)
        metadata = avatar_store.load(character_id)
        return {
            "character_id": character_id,
            "avatar_url": avatar_url(character_id),
            "avatar": metadata.model_dump(mode="json") if metadata is not None else None,
        }

    @app.get("/v1/characters/{character_id}/avatar/asset")
    def avatar_asset(character_id: str):
        profile(character_id)
        path = avatar_store.asset_path(character_id)
        if path is None:
            raise HTTPException(status_code=404, detail="avatar not found")
        metadata = avatar_store.load(character_id)
        return FileResponse(path, media_type=metadata.content_type if metadata is not None else None)

    @app.post("/v1/characters/{character_id}/avatar/search")
    def search_avatar(character_id: str, req: AvatarSearchRequest):
        item = profile(character_id)
        if provider_name != "brave":
            raise HTTPException(status_code=501, detail=f"Unsupported search_provider: {provider_name}")
        query = req.query.strip() or default_query(item)
        try:
            return {
                "character_id": character_id,
                **avatar_search.search(character_id, query, limit=req.limit),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            message = str(exc)
            status = 503 if "search_api_key" in message or "not configured" in message else 502
            raise HTTPException(status_code=status, detail=message) from exc

    @app.post("/v1/characters/{character_id}/avatar/select")
    def select_avatar(character_id: str, req: AvatarSelectRequest):
        profile(character_id)
        try:
            metadata = avatar_search.select(character_id, req.search_id, req.candidate_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "character_id": character_id,
            "avatar_url": avatar_url(character_id),
            "avatar": metadata.model_dump(mode="json"),
        }

    @app.on_event("shutdown")
    def _close_avatar_resources():
        avatar_search.close()
