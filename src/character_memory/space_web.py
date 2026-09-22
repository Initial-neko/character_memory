from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from character_memory.domain.models import SpaceMediaIntent, SpaceMediaIntentType
from character_memory.space_autonomy import SpaceAutonomyScheduler, SpaceAutonomyService, autonomy_enabled
from character_memory.space_media import MAX_SPACE_MEDIA_PER_POST, SpacePostMediaRepository
from character_memory.space_store import MAX_COMMENTERS_PER_POST, SpaceRepository


class CreateSpacePostRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    content: str = Field(default="", max_length=4000)
    # Legacy single-media input remains accepted while callers migrate to the
    # ordered media_ids contract.
    media_id: str | None = Field(default=None, max_length=120)
    media_ids: list[str] = Field(default_factory=list, max_length=MAX_SPACE_MEDIA_PER_POST)
    source_event_id: int | None = None

    @model_validator(mode="after")
    def require_content_or_media(self):
        self.content = self.content.strip()
        ordered: list[str] = []
        candidates = []
        if self.media_id:
            candidates.append(self.media_id)
        candidates.extend(self.media_ids)
        for value in candidates:
            media_id = str(value or "").strip()
            if len(media_id) > 120:
                raise ValueError("Space media_id must be at most 120 characters")
            if media_id and media_id not in ordered:
                ordered.append(media_id)
        if len(ordered) > MAX_SPACE_MEDIA_PER_POST:
            raise ValueError(f"a Space post may contain at most {MAX_SPACE_MEDIA_PER_POST} media items")
        self.media_ids = ordered
        self.media_id = ordered[0] if ordered else None
        if not self.content and not self.media_ids:
            raise ValueError("content or media_ids is required")
        return self


class CreateSpaceCommentRequest(BaseModel):
    # Omitted character_id means the human user is commenting from the browser.
    # Character-authored comments keep using an explicit, validated character id.
    character_id: str | None = Field(default=None, min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=1000)
    reply_to_comment_id: int | None = None

    @model_validator(mode="after")
    def clean_content(self):
        self.content = self.content.strip()
        if not self.content:
            raise ValueError("comment must not be empty")
        return self


class SpaceDevConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: float | None = Field(default=None, ge=10.0, le=10080.0)
    max_posts_per_day: int | None = Field(default=None, ge=0, le=200)
    media_enabled: bool | None = None
    media_max_items: int | None = Field(default=None, ge=0, le=9)
    image_search_enabled: bool | None = None
    image_generation_enabled: bool | None = None
    world_observation_enabled: bool | None = None
    world_max_pages: int | None = Field(default=None, ge=1, le=4)
    world_max_chars_per_page: int | None = Field(default=None, ge=500, le=16000)
    audience_size: int | None = Field(default=None, ge=0, le=10)
    poll_seconds: float | None = Field(default=None, ge=10.0, le=3600.0)
    rearm: bool = True


class SpaceDevMediaRequest(BaseModel):
    type: SpaceMediaIntentType
    content: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=9)
    query: str | None = Field(default=None, max_length=300)
    purpose: str | None = Field(default="SCENE", max_length=16)
    visual_intent: str | None = Field(default=None, max_length=800)
    voice_text: str | None = Field(default=None, max_length=4000)


def attach_space_routes(app):
    """Attach Character Space without initializing the LLM runtime."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before space routes are attached")

    repository = SpaceRepository(access.read_store)
    media_repository = SpacePostMediaRepository(access.read_store)
    autonomy = SpaceAutonomyService(access, repository)
    scheduler = SpaceAutonomyScheduler(
        access,
        repository,
        poll_seconds=float(getattr(access.settings, "space_scheduler_poll_seconds", 60.0)),
    )
    scheduler_capable = bool(getattr(access.settings, "api_key", ""))

    if scheduler_capable:
        @app.on_event("startup")
        def _start_space_autonomy():
            # Keep the scheduler thread alive even while autonomy is disabled so
            # Dev/Settings can hot-enable it without restarting Character Runtime.
            scheduler.start()

    @app.on_event("shutdown")
    def _stop_space_autonomy():
        scheduler.stop()
        autonomy.media_executor.close()

    # Expose the feature runtime for tests/diagnostics without initializing LLM.
    access.space_repository = repository
    access.space_media_repository = media_repository
    access.space_autonomy = autonomy
    access.space_scheduler = scheduler

    def repo() -> SpaceRepository:
        return repository

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

    def comment_author_payload(comment) -> dict:
        if comment.actor_type == "USER":
            return {
                "id": "user",
                "name": "我",
                "identity": "",
                "tagline": "",
                "avatar_url": "",
                "archived": False,
            }
        return profile_payload(comment.character_id)

    def relation_for_asset(asset) -> dict:
        mime_type = str(asset.mime_type or "").lower()
        if mime_type.startswith("image/"):
            media_type = "IMAGE"
        elif mime_type.startswith("audio/"):
            media_type = "VOICE"
        else:
            raise HTTPException(status_code=400, detail=f"unsupported Space media mime type: {asset.mime_type}")

        raw_source = str(asset.source or "").strip().upper()
        if "SEARCH" in raw_source:
            source_type = "SEARCH"
        elif "GENERATED" in raw_source or raw_source in {"IMAGEGEN", "AI_GENERATED", "TTS", "VOICE_SYNTH"}:
            source_type = "GENERATED"
        elif raw_source in {"WEB", "FETCHED", "WEB_FETCH"}:
            source_type = "WEB"
        else:
            source_type = "CHARACTER"

        return {
            "media_id": asset.id,
            "media_type": media_type,
            "source_type": source_type,
            "metadata": {"asset_source": asset.source},
        }

    def media_item_payload(item) -> dict:
        asset = access.read_store.get_media_asset(item.media_id)
        base = {
            "id": item.media_id,
            "media_id": item.media_id,
            "media_type": item.media_type,
            "source_type": item.source_type,
            "sort_order": item.sort_order,
            "metadata": item.metadata,
            "available": False,
            "url": None,
        }
        if asset is None:
            return base
        path = access.media_storage.asset_path(asset)
        if path is None:
            return {
                **base,
                "label": asset.original_name,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "asset_source": asset.source,
            }
        return {
            **base,
            "available": True,
            "label": asset.original_name,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "asset_source": asset.source,
            "url": f"/v1/media/{asset.id}",
        }

    def legacy_media_payload(media_id: str) -> dict | None:
        asset = access.read_store.get_media_asset(media_id)
        if asset is None:
            return None
        mime_type = str(asset.mime_type or "").lower()
        media_type = "IMAGE" if mime_type.startswith("image/") else "VOICE" if mime_type.startswith("audio/") else "IMAGE"
        relation = type(
            "LegacySpaceMedia",
            (),
            {
                "media_id": asset.id,
                "media_type": media_type,
                "source_type": "LEGACY",
                "sort_order": 0,
                "metadata": {"asset_source": asset.source},
            },
        )()
        return media_item_payload(relation)

    def post_payload(repository: SpaceRepository, post) -> dict:
        comments = repository.list_comments(post.id)
        likes = repository.list_reactions(post.id, "LIKE")
        media_items = [media_item_payload(item) for item in media_repository.list_for_post(post.id)]
        if not media_items and post.media_id:
            legacy = legacy_media_payload(post.media_id)
            if legacy is not None:
                media_items = [legacy]
        first_media = next((item for item in media_items if item.get("available")), None)
        commenter_ids = []
        for comment in comments:
            if comment.actor_type == "CHARACTER" and comment.character_id not in commenter_ids:
                commenter_ids.append(comment.character_id)
        return {
            "id": post.id,
            "character_id": post.character_id,
            "author": profile_payload(post.character_id),
            "content": post.content,
            "created_at": post.created_at.isoformat(),
            # Compatibility fields for old clients. New clients should consume
            # media_items, which is ordered and can contain up to nine assets.
            "media_id": post.media_id or (media_items[0]["media_id"] if media_items else None),
            "media": first_media,
            "media_items": media_items,
            "media_count": len(media_items),
            "media_limit": MAX_SPACE_MEDIA_PER_POST,
            "source_event_id": post.source_event_id,
            "visibility": post.visibility,
            "comments": [
                {
                    "id": item.id,
                    "post_id": item.post_id,
                    "character_id": item.character_id,
                    "actor_type": item.actor_type,
                    "author": comment_author_payload(item),
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
            "max_media_per_post": MAX_SPACE_MEDIA_PER_POST,
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
        relations: list[dict] = []
        for media_id in req.media_ids:
            asset = access.read_store.get_media_asset(media_id)
            if asset is None or access.media_storage.asset_path(asset) is None:
                raise HTTPException(status_code=400, detail=f"media not found: {media_id}")
            relations.append(relation_for_asset(asset))

        repository = repo()
        now = datetime.now().astimezone()
        try:
            post = repository.create_post(
                req.character_id,
                req.content,
                now,
                media_id=req.media_ids[0] if req.media_ids else None,
                source_event_id=req.source_event_id,
            )
            if relations:
                media_repository.replace_for_post(post.id, relations, now)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"post": post_payload(repository, post)}

    @app.post("/v1/space/posts/{post_id}/comments")
    def create_space_comment(post_id: int, req: CreateSpaceCommentRequest):
        actor_type = "CHARACTER" if req.character_id else "USER"
        commenter_id = req.character_id or "user"
        if req.character_id:
            require_known(req.character_id, active=True)
        repository = repo()
        try:
            comment = repository.add_comment(
                post_id,
                commenter_id,
                req.content,
                datetime.now().astimezone(),
                actor_type=actor_type,
                reply_to_comment_id=req.reply_to_comment_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409 if "at most" in str(exc) else 400, detail=str(exc)) from exc
        return {
            "comment": {
                **comment.model_dump(mode="json"),
                "author": comment_author_payload(comment),
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

    @app.get("/v1/space/dev/status")
    def space_dev_status():
        payload = scheduler.status()
        payload["scheduler_capable"] = scheduler_capable
        return payload

    @app.post("/v1/space/dev/config")
    def space_dev_config(req: SpaceDevConfigRequest):
        return scheduler.apply_runtime_config(
            enabled=req.enabled,
            interval_minutes=req.interval_minutes,
            max_posts_per_day=req.max_posts_per_day,
            media_enabled=req.media_enabled,
            media_max_items=req.media_max_items,
            image_search_enabled=req.image_search_enabled,
            image_generation_enabled=req.image_generation_enabled,
            world_observation_enabled=req.world_observation_enabled,
            world_max_pages=req.world_max_pages,
            world_max_chars_per_page=req.world_max_chars_per_page,
            audience_size=req.audience_size,
            poll_seconds=req.poll_seconds,
            rearm=req.rearm,
            now=datetime.now().astimezone(),
        )

    @app.post("/v1/space/dev/due/{character_id}")
    def space_dev_force_due(character_id: str):
        require_known(character_id, active=True)
        try:
            return {
                "character_id": character_id,
                "state": scheduler.force_due(
                    character_id,
                    now=datetime.now().astimezone(),
                ),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/space/dev/opportunity/{character_id}")
    def space_dev_opportunity(character_id: str):
        require_known(character_id, active=True)
        try:
            return autonomy.run_opportunity(
                character_id,
                now=datetime.now().astimezone(),
                cascade=True,
                source="DEV",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/space/dev/media/{character_id}")
    def space_dev_media(character_id: str, req: SpaceDevMediaRequest):
        """Force one explicit media intent without changing scheduler state."""
        require_known(character_id, active=True)
        now = datetime.now().astimezone()
        try:
            intent = SpaceMediaIntent.model_validate(req.model_dump())
            runtime = None
            if intent.type == SpaceMediaIntentType.GENERATE_IMAGE:
                bundle = access.require_bundle()
                runtime = getattr(bundle, "runtimes", {}).get(character_id)
                if runtime is None:
                    raise KeyError(f"runtime not found for character: {character_id}")
            result = autonomy.media_executor.execute(
                character_id,
                [intent],
                now=now,
                runtime=runtime,
            )
            relations = list(result["relations"])
            if not relations:
                raise RuntimeError(
                    (result["errors"][-1]["error"] if result["errors"] else "media executor returned no media")
                )
            post = repository.create_post(
                character_id,
                req.content.strip(),
                now,
                media_id=relations[0]["media_id"],
            )
            media_repository.replace_for_post(post.id, relations, now)
            return {
                "ok": True,
                "intent": intent.model_dump(mode="json"),
                "errors": result["errors"],
                "post": post_payload(repository, post),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/space/dev/audience/{post_id}")
    def space_dev_audience(post_id: int):
        try:
            return {
                "post_id": post_id,
                "audience": autonomy.process_audience(
                    post_id,
                    now=datetime.now().astimezone(),
                ),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
