from __future__ import annotations

from dataclasses import dataclass
import json

from character_memory.domain.models import Event, EventType
from character_memory.time_utils import parse_datetime


@dataclass(frozen=True)
class ChatHistoryPage:
    events: list[Event]
    has_more: bool
    next_before_id: int | None


class ChatHistoryRepository:
    """Read-optimized chat history queries kept separate from runtime writes."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _event_from_row(row) -> Event:
        return Event(
            id=row["id"],
            character_id=row["character_id"],
            event_type=EventType(row["event_type"]),
            event_time=parse_datetime(row["event_time"]),
            content=row["content"],
            metadata=json.loads(row["metadata_json"]),
        )

    def list_page(self, character_id: str, *, limit: int = 50, before_id: int | None = None) -> ChatHistoryPage:
        page_size = max(1, min(int(limit), 100))
        with self.store._lock:
            sql = (
                "SELECT * FROM events WHERE character_id=? AND event_time_epoch IS NOT NULL "
                "AND event_type IN (?,?)"
            )
            args: list = [character_id, EventType.USER_MESSAGE.value, EventType.CHARACTER_MESSAGE.value]
            if before_id is not None:
                cursor = self.store.conn.execute(
                    "SELECT event_time_epoch,id FROM events WHERE id=? AND character_id=?",
                    (int(before_id), character_id),
                ).fetchone()
                if cursor is None or cursor["event_time_epoch"] is None:
                    return ChatHistoryPage([], False, None)
                sql += " AND (event_time_epoch<? OR (event_time_epoch=? AND id<?))"
                args.extend([cursor["event_time_epoch"], cursor["event_time_epoch"], cursor["id"]])
            sql += " ORDER BY event_time_epoch DESC,id DESC LIMIT ?"
            args.append(page_size + 1)
            rows = self.store.conn.execute(sql, args).fetchall()

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        events = [self._event_from_row(row) for row in reversed(rows)]
        next_before_id = int(events[0].id) if has_more and events else None
        return ChatHistoryPage(events, has_more, next_before_id)

    def trace_sources(self, character_id: str, source_event_ids: list[int]) -> set[int]:
        ids = sorted({int(value) for value in source_event_ids if value is not None})
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self.store._lock:
            rows = self.store.conn.execute(
                f"SELECT source_event_id FROM runtime_traces WHERE character_id=? AND source_event_id IN ({placeholders})",
                [character_id, *ids],
            ).fetchall()
        return {int(row["source_event_id"]) for row in rows}
