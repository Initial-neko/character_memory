from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from character_memory.avatar_intent import AvatarIntentPlanner, AvatarSearchIntent
from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.config import load_persona, split_archived
from character_memory.domain.models import EventType


logger = logging.getLogger("character_memory.avatar_web")


class AvatarSearchRequest(BaseModel):
    # `hint` is an optional human preference, not the final search-engine query.
    # `query` stays as a compatibility alias for clients from the previous UI.
    hint: str = Field(default="", max_length=400)
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
    services = access.services
    avatar_store = services.avatar_store
    avatar_search = services.avatar_search
    provider = services.search_provider
    provider_name = str(getattr(settings, "search_provider", "searchapi") or "searchapi").strip().lower()

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

    def fallback_intent(item: dict[str, str], hint: str) -> AvatarSearchIntent:
        name = str(item.get("name") or item["id"]).strip()
        identity = str(item.get("identity") or "").strip()
        parts = [name]
        if identity and identity.casefold() not in name.casefold():
            parts.append(identity[:80])
        if hint:
            parts.append(hint[:160])
        parts.extend(["portrait", "avatar", "profile picture"])
        query = " ".join(part for part in parts if part).strip()[:180]
        return AvatarSearchIntent(
            visual_intent=f"保持{name}核心辨识度的清晰聊天头像",
            queries=[query],
            preferred_mood="",
            preferred_style="清晰人物主体、适合作为聊天头像",
        )

    def recent_dialogue(character_id: str) -> list[str]:
        try:
            events = access.read_store.list_chat_events(character_id, limit=8)
        except Exception:
            logger.exception("avatar.intent recent_dialogue failed character=%s", character_id)
            return []
        lines: list[str] = []
        for event in events[-8:]:
            if event.event_type == EventType.USER_MESSAGE:
                role = "用户"
                content = event.metadata.get("display_text", event.content)
            else:
                role = "角色"
                content = event.content
            text = " ".join(str(content or "").split()).strip()
            if text:
                lines.append(f"{role}: {text[:320]}")
        return lines

    def plan_search(character_id: str, item: dict[str, str], hint: str) -> tuple[AvatarSearchIntent, str]:
        try:
            current = access.require_bundle()
            runtime = getattr(current, "runtimes", {}).get(character_id)
            persona = getattr(runtime, "persona", "") if runtime is not None else ""
            if not persona:
                persona = load_persona(item["persona_path"])
            mental_state = access.read_store.get_mental_state(character_id)
            planner = AvatarIntentPlanner(current.model)
            intent = planner.plan(
                character_id,
                persona=persona,
                mental_state=mental_state,
                recent_dialogue=recent_dialogue(character_id),
                user_hint=hint,
            )
            logger.info(
                "avatar.intent planned character=%s queries=%d visual_intent_chars=%d",
                character_id,
                len(intent.queries),
                len(intent.visual_intent),
            )
            return intent, "llm"
        except Exception as exc:
            # Avatar search remains usable if the chat model is temporarily
            # unavailable. The old deterministic query is strictly a fallback.
            logger.warning("avatar.intent fallback character=%s error=%s", character_id, exc)
            return fallback_intent(item, hint), "fallback"

    @app.get("/v1/character-profiles")
    def character_profiles_with_avatars(archived: bool = False):
        # Filtered like ``/v1/characters``: the avatar manager republishes this
        # list straight into ``CM.state.characters``, so a route that answered
        # with archived characters would put them back in the sidebar.
        listed = split_archived(access.character_profiles(), archived)
        return {"characters": [public_profile(item) for item in listed]}

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
        if provider is None:
            raise HTTPException(status_code=501, detail=f"Unsupported search_provider: {provider_name}")
        hint = req.hint.strip() or req.query.strip()
        intent, planning_source = plan_search(character_id, item, hint)
        try:
            result = avatar_search.search_queries(character_id, intent.queries, limit=req.limit)
            return {
                "character_id": character_id,
                **result,
                "visual_intent": intent.visual_intent,
                "preferred_mood": intent.preferred_mood,
                "preferred_style": intent.preferred_style,
                "planning_source": planning_source,
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
