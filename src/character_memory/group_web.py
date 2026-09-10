from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import threading
import time

from pydantic import BaseModel, Field, model_validator

from character_memory.app import build_app
from character_memory.application.group_conversation_service import GroupConversationService
from character_memory.config import discover_character_profiles, load_settings, resolve_media_dir
from character_memory.group_store import GroupRepository
from character_memory.images import load_image_catalog
from character_memory.media import MediaStorage
from character_memory.stickers import load_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


logger = logging.getLogger("character_memory.group_web")


class CreateGroupRequest(BaseModel):
    name: str = Field(default="新群聊", max_length=80)
    member_ids: list[str] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def distinct_members(self):
        cleaned = [str(value).strip() for value in self.member_ids if str(value).strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("group members must be distinct")
        self.member_ids = cleaned
        return self


class GroupImageRequest(BaseModel):
    filename: str = Field(default="image", min_length=1, max_length=180)
    data_url: str = Field(min_length=16)


class GroupChatRequest(BaseModel):
    message: str = Field(default="", max_length=12000)
    image: GroupImageRequest | None = None
    at: datetime | None = None

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and self.image is None:
            raise ValueError("message or image is required")
        return self


class GroupRuntimeManager:
    """Lazy P0.11 runtime pool.

    The legacy API owns an internal lazy bundle that is not yet exposed through
    app.state. P0.11 therefore keeps its own lazy bundle as a temporary V0
    compatibility layer. Group facts still live in the same SQLite database.
    A future API composition refactor should expose one shared bundle holder.
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.settings = load_settings(config_path)
        self.read_store = SQLiteStore(self.settings.db_path)
        self.bundle = None
        self.lock = threading.RLock()
        self.media = MediaStorage(
            resolve_media_dir(self.settings),
            max_bytes=int(getattr(self.settings, "media_max_bytes", 8 * 1024 * 1024)),
        )

    def profiles(self) -> list[dict[str, str]]:
        return discover_character_profiles(self.settings)

    def store(self):
        return self.bundle.store if self.bundle is not None else self.read_store

    def get_bundle(self):
        with self.lock:
            profile_ids = {item["id"] for item in self.profiles()}
            if self.bundle is not None and set(self.bundle.runtimes) != profile_ids:
                logger.info("group.runtime character_set_changed rebuild=true")
                self.bundle.close()
                self.bundle = None
            if self.bundle is None:
                started = time.perf_counter()
                logger.info("group.runtime lazy_init start")
                self.bundle = build_app(self.config_path)
                logger.info("group.runtime lazy_init done characters=%d total_ms=%.1f", len(self.bundle.runtimes), (time.perf_counter() - started) * 1000)
            return self.bundle

    def refresh_character_resources(self, member_ids: list[str]) -> None:
        if self.bundle is None:
            return
        profiles = {item["id"]: item for item in self.profiles()}
        for character_id in member_ids:
            profile = profiles.get(character_id)
            runtime = self.bundle.runtimes.get(character_id)
            if profile is None or runtime is None:
                continue
            runtime.sticker_catalog = load_sticker_catalog(profile["persona_path"])
            runtime.image_catalog = load_image_catalog(profile["persona_path"])

    def close(self) -> None:
        if self.bundle is not None:
            self.bundle.close()
        self.read_store.close()


def attach_group_routes(app, config_path: str = "config.yaml"):
    """Attach P0.11 routes to the existing FastAPI app without touching 1:1 API."""
    from fastapi import HTTPException

    manager = GroupRuntimeManager(config_path)

    def profiles_by_id() -> dict[str, dict[str, str]]:
        return {item["id"]: item for item in manager.profiles()}

    def repo() -> GroupRepository:
        return GroupRepository(manager.store())

    def group_payload(group) -> dict:
        profiles = profiles_by_id()
        return {
            "id": group.id,
            "name": group.name,
            "member_ids": group.member_ids,
            "members": [
                {
                    "id": character_id,
                    "name": profiles.get(character_id, {}).get("name") or character_id,
                    "identity": profiles.get(character_id, {}).get("identity") or "",
                }
                for character_id in group.member_ids
            ],
            "created_at": group.created_at.isoformat(),
            "updated_at": group.updated_at.isoformat(),
        }

    def sticker_payload(character_id: str, sticker_id: str | None) -> dict | None:
        if not sticker_id:
            return None
        profile = profiles_by_id().get(character_id)
        if profile is None:
            return None
        catalog = load_sticker_catalog(profile["persona_path"])
        sticker = catalog.get(sticker_id)
        if sticker is None or catalog.asset_path(sticker_id) is None:
            return None
        return {
            **sticker.model_dump(mode="json"),
            "url": f"/v1/stickers/{character_id}/{sticker.id}/asset",
        }

    def image_payload(character_id: str, image_id: str | None) -> dict | None:
        if not image_id:
            return None
        profile = profiles_by_id().get(character_id)
        if profile is None:
            return None
        catalog = load_image_catalog(profile["persona_path"])
        image = catalog.get(image_id)
        if image is None or catalog.asset_path(image_id) is None:
            return None
        return {
            **image.model_dump(mode="json"),
            "source": "CHARACTER_LIBRARY",
            "url": f"/v1/images/{character_id}/{image.id}/asset",
        }

    def uploaded_media_payload(media_id: str | None) -> dict | None:
        if not media_id:
            return None
        asset = manager.store().get_media_asset(media_id)
        if asset is None or manager.media.asset_path(asset) is None:
            return None
        return {
            "id": asset.id,
            "label": asset.original_name,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "source": asset.source,
            "url": f"/v1/media/{asset.id}",
        }

    def event_payload(event) -> dict:
        profile = profiles_by_id().get(event.actor_id, {})
        role = "user" if event.actor_type == "USER" else "assistant"
        content = event.metadata.get("display_text", event.content) if role == "user" else event.content
        sticker = sticker_payload(event.actor_id, event.metadata.get("sticker_id")) if role == "assistant" else None
        image = image_payload(event.actor_id, event.metadata.get("image_id")) if role == "assistant" else None
        if event.metadata.get("media_id"):
            image = uploaded_media_payload(event.metadata.get("media_id"))
        return {
            "id": event.id,
            "conversation_id": event.conversation_id,
            "turn_id": event.turn_id,
            "role": role,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "actor_name": "我" if role == "user" else (profile.get("name") or event.actor_id),
            "content": content,
            "event_time": event.event_time.isoformat(),
            "action": event.metadata.get("action"),
            "sticker_id": event.metadata.get("sticker_id"),
            "sticker": sticker,
            "image_id": event.metadata.get("image_id"),
            "media_id": event.metadata.get("media_id"),
            "image": image,
            "source_conversation_event_id": event.metadata.get("source_conversation_event_id"),
        }

    @app.get("/v1/groups")
    def list_groups():
        return {"groups": [group_payload(group) for group in repo().list_groups()]}

    @app.post("/v1/groups")
    def create_group(req: CreateGroupRequest):
        known = profiles_by_id()
        unknown = [character_id for character_id in req.member_ids if character_id not in known]
        if unknown:
            raise HTTPException(status_code=404, detail=f"Unknown characters: {', '.join(unknown)}")
        now = datetime.now().astimezone()
        # Group creation must remain cheap and must not initialize the model.
        try:
            group = repo().create_group(req.name, req.member_ids, now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("group.created id=%s members=%s", group.id, group.member_ids)
        return {"group": group_payload(group)}

    @app.get("/v1/groups/{conversation_id}/history")
    def group_history(conversation_id: str, limit: int = 180):
        repository = repo()
        group = repository.get_group(conversation_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        events = repository.list_events(conversation_id, limit=max(1, min(limit, 500)))
        return {"group": group_payload(group), "messages": [event_payload(event) for event in events]}

    @app.get("/v1/groups/{conversation_id}/turns/{turn_id}/traces")
    def group_turn_traces(conversation_id: str, turn_id: str):
        repository = repo()
        if repository.get_group(conversation_id) is None:
            raise HTTPException(status_code=404, detail="group not found")
        traces = repository.list_turn_traces(conversation_id, turn_id)
        # Only developer-safe summaries are exposed here. No hidden chain-of-thought.
        return {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "traces": [
                {
                    "character_id": item.get("character_id"),
                    "character_name": profiles_by_id().get(item.get("character_id"), {}).get("name") or item.get("character_id"),
                    "perception": item.get("perception", ""),
                    "reaction": item.get("reaction", ""),
                    "actions": item.get("actions", []),
                    "created_memory_ids": item.get("created_memory_ids", []),
                    "model_used": item.get("model_used", ""),
                    "model_ms": item.get("model_ms", 0),
                    "total_ms": item.get("total_ms", 0),
                }
                for item in traces
            ],
        }

    @app.post("/v1/groups/{conversation_id}/chat")
    def group_chat(conversation_id: str, req: GroupChatRequest):
        preliminary = repo().get_group(conversation_id)
        if preliminary is None:
            raise HTTPException(status_code=404, detail="group not found")
        try:
            bundle = manager.get_bundle()
            manager.refresh_character_resources(preliminary.member_ids)
            service = GroupConversationService(
                bundle.store,
                bundle.runtimes,
                bundle.clock,
                chat_service=bundle.chat,
                profiles=manager.profiles(),
            )
            selected_image = None
            image_data_url = None
            if req.image is not None:
                upload_time = req.at or datetime.now().astimezone()
                asset, image_data_url = manager.media.save_data_url(
                    character_id=f"group:{conversation_id}",
                    original_name=req.image.filename,
                    data_url=req.image.data_url,
                    created_at=upload_time,
                )
                bundle.store.add_media_asset(asset)
                selected_image = asset.model_dump(mode="json")
            result = service.send(
                conversation_id,
                req.message,
                at=req.at,
                image=selected_image,
                image_data_url=image_data_url,
            )
            group = service.repo.get_group(conversation_id)
            events = service.repo.list_events(conversation_id, limit=180)
            return {
                "conversation_id": conversation_id,
                "turn_id": result["turn_id"],
                "speaker_order": result["speaker_order"],
                "decisions": result["decisions"],
                "group": group_payload(group),
                "messages": [event_payload(event) for event in events],
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("group.chat failed conversation=%s error=%s", conversation_id, exc)
            raise HTTPException(status_code=500, detail=f"群聊生成失败：{exc}") from exc

    @app.on_event("shutdown")
    def _shutdown_group_runtime():
        manager.close()

    app.state.group_runtime_manager = manager
    return app
