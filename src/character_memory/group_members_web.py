from __future__ import annotations

from datetime import datetime
import threading

from pydantic import BaseModel, Field, model_validator

from character_memory.group_store import GroupRepository, MAX_GROUP_CHARACTERS
from character_memory.time_utils import epoch_us


# The repository speaks English identifiers; this module is the HTTP boundary the
# user actually reads, and the rest of the group UI is Chinese. An unmapped
# message falls through unchanged rather than being swallowed, so a new store
# error stays legible on the wire instead of turning into a generic failure.
_REMOVE_MEMBER_ERROR_DETAILS = {
    "group must retain at least 2 characters": "群聊需保留至少 2 个 Character，无法移出。",
}


class AddGroupMembersRequest(BaseModel):
    member_ids: list[str] = Field(min_length=1, max_length=MAX_GROUP_CHARACTERS)

    @model_validator(mode="after")
    def clean_members(self):
        cleaned: list[str] = []
        for raw in self.member_ids:
            value = str(raw).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            raise ValueError("at least one character is required")
        self.member_ids = cleaned
        return self


def attach_group_member_routes(app):
    """Attach the minimal mutable-membership API for existing group chats."""
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before group member routes are attached")

    locks_guard = threading.Lock()
    locks: dict[str, threading.RLock] = {}

    def turn_lock_for(conversation_id: str) -> threading.RLock:
        scheduler = getattr(access, "reaction_scheduler", None)
        if scheduler is not None:
            return scheduler.group_lock_for(conversation_id)
        with locks_guard:
            lock = locks.get(conversation_id)
            if lock is None:
                lock = threading.RLock()
                locks[conversation_id] = lock
            return lock

    def profiles_by_id() -> dict[str, dict[str, str]]:
        return {item["id"]: item for item in access.character_profiles()}

    def payload(group, profiles: dict[str, dict[str, str]]) -> dict:
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
            "archived_at": group.archived_at.isoformat() if group.archived_at else None,
        }

    @app.post("/v1/groups/{conversation_id}/members")
    def add_group_members(conversation_id: str, req: AddGroupMembersRequest):
        repository = GroupRepository(access.store())
        profiles = profiles_by_id()
        unknown = [character_id for character_id in req.member_ids if character_id not in profiles]
        if unknown:
            raise HTTPException(status_code=404, detail=f"Unknown characters: {', '.join(unknown)}")

        with turn_lock_for(conversation_id):
            group = repository.get_group(conversation_id)
            if group is None:
                raise HTTPException(status_code=404, detail="group not found")

            additions = [character_id for character_id in req.member_ids if character_id not in group.member_ids]
            merged = [*group.member_ids, *additions]
            if len(merged) > MAX_GROUP_CHARACTERS:
                raise HTTPException(status_code=400, detail=f"group supports at most {MAX_GROUP_CHARACTERS} characters")

            if additions:
                now = datetime.now().astimezone()
                stamp = epoch_us(now)
                with repository.store.transaction():
                    next_position = len(group.member_ids) + 1
                    for offset, character_id in enumerate(additions):
                        repository.store.conn.execute(
                            "INSERT INTO conversation_members(conversation_id,actor_type,actor_id,position,joined_at,joined_at_epoch) VALUES(?,?,?,?,?,?)",
                            (conversation_id, "CHARACTER", character_id, next_position + offset, now.isoformat(), stamp),
                        )
                    repository.store.conn.execute(
                        "UPDATE conversations SET updated_at=?,updated_at_epoch=? WHERE id=? AND type='GROUP' AND archived_at IS NULL",
                        (now.isoformat(), stamp, conversation_id),
                    )

            updated = repository.get_group(conversation_id)
            if updated is None:
                raise HTTPException(status_code=404, detail="group not found")
            return {"group": payload(updated, profiles), "added_member_ids": additions}

    @app.delete("/v1/groups/{conversation_id}/members/{character_id}")
    def remove_group_member(conversation_id: str, character_id: str):
        repository = GroupRepository(access.store())
        profiles = profiles_by_id()
        character_id = str(character_id).strip()
        if character_id not in profiles:
            raise HTTPException(status_code=404, detail="character not found")

        with turn_lock_for(conversation_id):
            try:
                updated = repository.remove_member(
                    conversation_id,
                    character_id,
                    datetime.now().astimezone(),
                )
            except KeyError as exc:
                if str(exc).strip('"') == "group not found":
                    raise HTTPException(status_code=404, detail="group not found") from exc
                raise HTTPException(status_code=404, detail="character is not a group member") from exc
            except ValueError as exc:
                message = str(exc)
                raise HTTPException(
                    status_code=400,
                    detail=_REMOVE_MEMBER_ERROR_DETAILS.get(message, message),
                ) from exc

            return {
                "group": payload(updated, profiles),
                "removed_member_id": character_id,
            }

    return app
