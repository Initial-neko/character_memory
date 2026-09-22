from __future__ import annotations

import json

from character_memory.domain.models import EventType


class MessageSearchRepository:
    """Deterministic text search over durable chat Event Logs.

    Search intentionally targets persisted chat events only. Memory, runtime
    traces, diary-like derived data, and mental state are not part of message
    search. The first version uses parameterized SQLite LIKE queries; FTS can be
    introduced later without changing the HTTP contract.
    """

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _pattern(query: str) -> str:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _metadata(row) -> dict:
        try:
            return json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            return {}

    def search_direct(self, query: str, *, character_id: str | None = None, limit: int = 50) -> list[dict]:
        pattern = self._pattern(query.strip())
        page_size = max(1, min(int(limit), 50))
        with self.store._lock:
            sql = (
                "SELECT id,character_id,event_type,event_time,event_time_epoch,content,metadata_json "
                "FROM events WHERE event_time_epoch IS NOT NULL AND event_type IN (?,?) "
            )
            args: list = [EventType.USER_MESSAGE.value, EventType.CHARACTER_MESSAGE.value]
            if character_id:
                sql += "AND character_id=? "
                args.append(character_id)
            sql += (
                "AND (COALESCE(json_extract(metadata_json,'$.display_text'),'') || ' ' || "
                "COALESCE(content,'') || ' ' || COALESCE(json_extract(metadata_json,'$.sticker_label'),'') || ' ' || "
                "COALESCE(json_extract(metadata_json,'$.media_name'),'') || ' ' || COALESCE(json_extract(metadata_json,'$.image_label'),'')) "
                "LIKE ? ESCAPE '\\' ORDER BY event_time_epoch DESC,id DESC LIMIT ?"
            )
            args.extend([pattern, page_size])
            rows = self.store.conn.execute(sql, args).fetchall()
        result = []
        for row in rows:
            metadata = self._metadata(row)
            content = metadata.get("display_text", row["content"]) if row["event_type"] == EventType.USER_MESSAGE.value else row["content"]
            result.append(
                {
                    "scope": "DIRECT",
                    "event_id": int(row["id"]),
                    "character_id": str(row["character_id"]),
                    "conversation_id": metadata.get("conversation_id"),
                    "event_type": str(row["event_type"]),
                    "event_time": str(row["event_time"]),
                    "event_time_epoch": int(row["event_time_epoch"]),
                    "content": str(content or ""),
                    "metadata": metadata,
                }
            )
        return result

    def search_group(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[dict]:
        pattern = self._pattern(query.strip())
        page_size = max(1, min(int(limit), 50))
        with self.store._lock:
            sql = (
                "SELECT e.id,e.conversation_id,e.turn_id,e.actor_type,e.actor_id,e.event_type,e.event_time,e.event_time_epoch,e.content,e.metadata_json,c.name AS conversation_name "
                "FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE e.event_time_epoch IS NOT NULL AND e.actor_type IN ('USER','CHARACTER') "
            )
            args: list = []
            if not include_archived:
                sql += "AND c.archived_at IS NULL "
            if conversation_id:
                sql += "AND e.conversation_id=? "
                args.append(conversation_id)
            sql += (
                "AND (COALESCE(json_extract(e.metadata_json,'$.display_text'),'') || ' ' || "
                "COALESCE(e.content,'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.sticker_label'),'') || ' ' || "
                "COALESCE(json_extract(e.metadata_json,'$.media_name'),'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.image_label'),'')) "
                "LIKE ? ESCAPE '\\' ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT ?"
            )
            args.extend([pattern, page_size])
            rows = self.store.conn.execute(sql, args).fetchall()
        result = []
        for row in rows:
            metadata = self._metadata(row)
            content = metadata.get("display_text", row["content"]) if row["actor_type"] == "USER" else row["content"]
            result.append(
                {
                    "scope": "GROUP",
                    "event_id": int(row["id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "conversation_name": str(row["conversation_name"]),
                    "turn_id": str(row["turn_id"]),
                    "actor_type": str(row["actor_type"]),
                    "actor_id": str(row["actor_id"]),
                    "event_type": str(row["event_type"]),
                    "event_time": str(row["event_time"]),
                    "event_time_epoch": int(row["event_time_epoch"]),
                    "content": str(content or ""),
                    "metadata": metadata,
                }
            )
        return result

    def jump_before_direct(self, character_id: str, event_id: int, *, newer_context: int = 20) -> int | None:
        with self.store._lock:
            target = self.store.conn.execute(
                "SELECT event_time_epoch,id FROM events WHERE id=? AND character_id=? AND event_time_epoch IS NOT NULL",
                (int(event_id), character_id),
            ).fetchone()
            if target is None:
                return None
            rows = self.store.conn.execute(
                "SELECT id FROM events WHERE character_id=? AND event_time_epoch IS NOT NULL AND event_type IN (?,?) "
                "AND (event_time_epoch>? OR (event_time_epoch=? AND id>?)) "
                "ORDER BY event_time_epoch ASC,id ASC LIMIT ?",
                (
                    character_id,
                    EventType.USER_MESSAGE.value,
                    EventType.CHARACTER_MESSAGE.value,
                    target["event_time_epoch"],
                    target["event_time_epoch"],
                    target["id"],
                    max(1, int(newer_context)),
                ),
            ).fetchall()
        if len(rows) < newer_context:
            return None
        return int(rows[-1]["id"])

    def jump_before_group(self, conversation_id: str, event_id: int, *, newer_context: int = 20) -> int | None:
        with self.store._lock:
            target = self.store.conn.execute(
                "SELECT event_time_epoch,id FROM conversation_events WHERE id=? AND conversation_id=? "
                "AND event_time_epoch IS NOT NULL AND actor_type IN ('USER','CHARACTER')",
                (int(event_id), conversation_id),
            ).fetchone()
            if target is None:
                return None
            rows = self.store.conn.execute(
                "SELECT id FROM conversation_events WHERE conversation_id=? AND event_time_epoch IS NOT NULL "
                "AND actor_type IN ('USER','CHARACTER') "
                "AND (event_time_epoch>? OR (event_time_epoch=? AND id>?)) "
                "ORDER BY event_time_epoch ASC,id ASC LIMIT ?",
                (
                    conversation_id,
                    target["event_time_epoch"],
                    target["event_time_epoch"],
                    target["id"],
                    max(1, int(newer_context)),
                ),
            ).fetchall()
        if len(rows) < newer_context:
            return None
        return int(rows[-1]["id"])
