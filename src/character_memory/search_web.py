from __future__ import annotations

from character_memory.storage.message_search import MessageSearchRepository


def attach_search_routes(app):
    """Attach deterministic Event Log message-search routes."""
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before search routes are attached")

    def profiles_by_id() -> dict[str, dict]:
        return {item["id"]: item for item in access.character_profiles()}

    def display_preview(item: dict) -> str:
        content = str(item.get("content") or "").strip()
        metadata = item.get("metadata") or {}
        if content:
            return content
        if metadata.get("sticker_label"):
            return f"[表情包] {metadata['sticker_label']}"
        media_name = metadata.get("media_name") or metadata.get("image_label")
        if media_name:
            return f"[图片] {media_name}"
        return "[空消息]"

    def decorate(repository: MessageSearchRepository, item: dict, profiles: dict[str, dict]) -> dict:
        scope = item["scope"]
        if scope == "DIRECT":
            character_id = item["character_id"]
            profile = profiles.get(character_id) or {}
            is_user = item.get("event_type") == "USER_MESSAGE"
            return {
                "scope": scope,
                "event_id": item["event_id"],
                "character_id": character_id,
                "conversation_id": item.get("conversation_id"),
                "conversation_name": profile.get("name") or character_id,
                "actor_type": "USER" if is_user else "CHARACTER",
                "actor_id": "user" if is_user else character_id,
                "actor_name": "我" if is_user else (profile.get("name") or character_id),
                "event_time": item["event_time"],
                "preview": display_preview(item),
                "jump_before_id": repository.jump_before_direct(character_id, item["event_id"]),
            }

        actor_id = item.get("actor_id") or ""
        is_user = item.get("actor_type") == "USER"
        profile = profiles.get(actor_id) or {}
        return {
            "scope": scope,
            "event_id": item["event_id"],
            "conversation_id": item["conversation_id"],
            "conversation_name": item.get("conversation_name") or item["conversation_id"],
            "turn_id": item.get("turn_id"),
            "actor_type": item.get("actor_type"),
            "actor_id": actor_id,
            "actor_name": "我" if is_user else (profile.get("name") or actor_id),
            "event_time": item["event_time"],
            "preview": display_preview(item),
            "jump_before_id": repository.jump_before_group(item["conversation_id"], item["event_id"]),
        }

    @app.get("/v1/search/messages")
    def search_messages(
        q: str,
        scope: str = "global",
        character_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 50,
    ):
        query = q.strip()
        if not query:
            raise HTTPException(status_code=400, detail="search query must not be empty")
        if len(query) > 200:
            raise HTTPException(status_code=400, detail="search query is too long")
        page_size = max(1, min(int(limit), 50))
        normalized = scope.strip().lower()
        repository = MessageSearchRepository(access.store())

        if normalized == "direct":
            if not character_id:
                raise HTTPException(status_code=400, detail="character_id is required for direct search")
            raw = repository.search_direct(query, character_id=character_id, limit=page_size)
        elif normalized == "group":
            if not conversation_id:
                raise HTTPException(status_code=400, detail="conversation_id is required for group search")
            raw = repository.search_group(query, conversation_id=conversation_id, limit=page_size)
        elif normalized == "global":
            raw = repository.search_direct(query, limit=page_size) + repository.search_group(query, limit=page_size)
            raw.sort(key=lambda item: (int(item.get("event_time_epoch") or 0), int(item.get("event_id") or 0)), reverse=True)
            raw = raw[:page_size]
        else:
            raise HTTPException(status_code=400, detail="scope must be global, direct or group")

        profiles = profiles_by_id()
        results = [decorate(repository, item, profiles) for item in raw]
        return {
            "query": query,
            "scope": normalized,
            "count": len(results),
            "results": results,
        }

    return app
