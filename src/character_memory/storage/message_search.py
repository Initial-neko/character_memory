from __future__ import annotations

import json

from character_memory.domain.models import EventType
from character_memory.storage.conversation_facts import ConversationFactReader


class MessageSearchRepository:
    """Deterministic text search over durable chat Event Logs.

    Search intentionally targets persisted chat events only. Memory, runtime
    traces, diary-like derived data, and mental state are not part of message
    search. The first version uses parameterized SQLite LIKE queries; FTS can be
    introduced later without changing the HTTP contract.

    Direct hits come from the unified conversation-fact domain (see
    ``ConversationFactReader``); Group hits come from the shared group domain.
    Global search concatenates the two scopes and never merges a Direct row into
    the Group query, which is what kept ``scope='GROUP'`` from mislabelling
    Direct facts.
    """

    def __init__(self, store):
        self.store = store
        self.facts = ConversationFactReader(store)

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
        return self.facts.search(query, character_id=character_id, limit=limit)

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
            # Direct facts share this table after W3; a group query must name the
            # GROUP domain explicitly or it will return Direct rows as group ones.
            sql = (
                "SELECT e.id,e.conversation_id,e.turn_id,e.actor_type,e.actor_id,e.event_type,e.event_time,e.event_time_epoch,e.content,e.metadata_json,c.name AS conversation_name "
                "FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE c.type='GROUP' AND e.event_time_epoch IS NOT NULL AND e.actor_type IN ('USER','CHARACTER') "
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
        return self.facts.jump_before(character_id, event_id, newer_context=newer_context)

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

