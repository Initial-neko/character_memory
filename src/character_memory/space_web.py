from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from character_memory.space_store import MAX_COMMENTERS_PER_POST, SpaceRepository


class CreateSpacePostRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    content: str = Field(default="", max_length=4000)
    media_id: str | None = Field(default=None, max_length=120)
    source_event_id: int | None = None

    @model_validator(mode="after")
    def require_content_or_media(self):
        self.content = self.content.strip()
        self.media_id = (self.media_id or "").strip() or None
        if not self.content and self.media_id is None:
            raise ValueError("content or media_id is required")
        return self


class CreateSpaceCommentRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=1000)
    reply_to_comment_id: int | None = None

    @model_validator(mode="after")
    def clean_content(self):
        self.content = self.content.strip()
        if not self.content:
            raise ValueError("comment must not be empty")
        return self


def attach_space_routes(app):
    """Attach Character Space without initializing the LLM runtime."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before space routes are attached")

    def repo() -> SpaceRepository:
        return SpaceRepository(access.store())

    def profiles_by_id() -> dict[str, dict]:
        return {item["id"]: item for item in access.character_profiles()}

    def require_known(character_id: str, *, active: bool = False) -> dict:
        profile = profiles_by_id().get(character_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="character not found")
        if active and "archived_at" in profile:
            raise HTTPException(status_code=409, detail="archived characters cannot create new Space interactions")
        return profile

    def profile_payload(character_id: str) -> dict:
        profile = profiles_by_id().get(character_id) or {"id": character_id, "name": character_id}
        return {
            "id": character_id,
            "name": profile.get("name") or character_id,
            "identity": profile.get("identity") or "",
            "tagline": profile.get("tagline") or "",
            "avatar_url": profile.get("avatar_url") or "",
            "archived": "archived_at" in profile,
        }

    def media_payload(media_id: str | None) -> dict | None:
        if not media_id:
            return None
        asset = access.store().get_media_asset(media_id)
        if asset is None or access.media_storage.asset_path(asset) is None:
            return None
        return {
            "id": asset.id,
            "label": asset.original_name,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "url": f"/v1/media/{asset.id}",
        }

    def post_payload(repository: SpaceRepository, post) -> dict:
        comments = repository.list_comments(post.id)
        likes = repository.list_reactions(post.id, "LIKE")
        commenter_ids = []
        for comment in comments:
            if comment.character_id not in commenter_ids:
                commenter_ids.append(comment.character_id)
        return {
            "id": post.id,
            "character_id": post.character_id,
            "author": profile_payload(post.character_id),
            "content": post.content,
            "created_at": post.created_at.isoformat(),
            "media_id": post.media_id,
            "media": media_payload(post.media_id),
            "source_event_id": post.source_event_id,
            "visibility": post.visibility,
            "comments": [
                {
                    "id": item.id,
                    "post_id": item.post_id,
                    "character_id": item.character_id,
                    "author": profile_payload(item.character_id),
                    "content": item.content,
                    "created_at": item.created_at.isoformat(),
                    "reply_to_comment_id": item.reply_to_comment_id,
                }
                for item in comments
            ],
            "commenter_count": len(commenter_ids),
            "commenter_limit": MAX_COMMENTERS_PER_POST,
            "like_count": len(likes),
            "likes": [
                {
                    "character_id": item.character_id,
                    "character": profile_payload(item.character_id),
                    "created_at": item.created_at.isoformat(),
                }
                for item in likes[:10]
            ],
        }

    @app.get("/v1/space/posts")
    def list_space_posts(character_id: str | None = None, limit: int = 10, before_id: int | None = None):
        if character_id:
            require_known(character_id)
        repository = repo()
        posts, has_more, next_before_id = repository.list_posts(
            character_id=character_id,
            limit=max(1, min(int(limit), 10)),
            before_id=before_id,
        )
        active_count = sum(1 for item in access.character_profiles() if "archived_at" not in item)
        return {
            "posts": [post_payload(repository, item) for item in posts],
            "total": repository.count_posts(character_id=character_id),
            "has_more": has_more,
            "next_before_id": next_before_id,
            "character_id": character_id,
            "active_character_count": active_count,
            "max_feed_items": 10,
        }

    @app.get("/v1/space/posts/{post_id}")
    def get_space_post(post_id: int):
        repository = repo()
        post = repository.get_post(post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="space post not found")
        return {"post": post_payload(repository, post)}

    @app.post("/v1/space/posts")
    def create_space_post(req: CreateSpacePostRequest):
        require_known(req.character_id, active=True)
        if req.media_id:
            asset = access.store().get_media_asset(req.media_id)
            if asset is None or access.media_storage.asset_path(asset) is None:
                raise HTTPException(status_code=400, detail="media not found")
        repository = repo()
        try:
            post = repository.create_post(
                req.character_id,
                req.content,
                datetime.now().astimezone(),
                media_id=req.media_id,
                source_event_id=req.source_event_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"post": post_payload(repository, post)}

    @app.post("/v1/space/posts/{post_id}/comments")
    def create_space_comment(post_id: int, req: CreateSpaceCommentRequest):
        require_known(req.character_id, active=True)
        repository = repo()
        try:
            comment = repository.add_comment(
                post_id,
                req.character_id,
                req.content,
                datetime.now().astimezone(),
                reply_to_comment_id=req.reply_to_comment_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409 if "at most" in str(exc) else 400, detail=str(exc)) from exc
        return {
            "comment": {
                **comment.model_dump(mode="json"),
                "author": profile_payload(comment.character_id),
            },
            "post": post_payload(repository, repository.get_post(post_id)),
        }

    @app.put("/v1/space/posts/{post_id}/likes/{character_id}")
    def like_space_post(post_id: int, character_id: str):
        require_known(character_id, active=True)
        repository = repo()
        try:
            repository.set_reaction(post_id, character_id, "LIKE", True, datetime.now().astimezone())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"post": post_payload(repository, repository.get_post(post_id))}

    @app.delete("/v1/space/posts/{post_id}/likes/{character_id}")
    def unlike_space_post(post_id: int, character_id: str):
        require_known(character_id, active=True)
        repository = repo()
        try:
            repository.set_reaction(post_id, character_id, "LIKE", False, datetime.now().astimezone())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"post": post_payload(repository, repository.get_post(post_id))}

    @app.put("/v1/space/posts/{post_id}/views/{character_id}")
    def record_space_view(post_id: int, character_id: str):
        require_known(character_id, active=True)
        repository = repo()
        try:
            view = repository.record_view(post_id, character_id, datetime.now().astimezone())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"view": view.model_dump(mode="json")}

    return app
