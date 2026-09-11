from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from character_memory.time_utils import epoch_us, parse_datetime


class GroupConversation(BaseModel):
    id: str
    name: str
    member_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class GroupEvent(BaseModel):
    id: int | None = None
    conversation_id: str
    turn_id: str
    actor_type: str
    actor_id: str
    event_type: str
    event_time: datetime
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GroupRepository:
    """Storage for shared group facts.

    Group events intentionally do not reuse the character-local `events` table.
    The same group fact must exist only once even though several characters may
    observe and remember it differently.
    """

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _migrate_epoch_keys(self) -> tuple[int, int]:
        specs = [
            ("conversations", "created_at", "created_at_epoch"),
            ("conversations", "updated_at", "updated_at_epoch"),
            ("conversation_members", "joined_at", "joined_at_epoch"),
            ("conversation_events", "event_time", "event_time_epoch"),
            ("conversation_runtime_traces", "created_at", "created_at_epoch"),
        ]
        migrated = 0
        invalid = 0
        for table, time_column, epoch_column in specs:
            self.store._ensure_column_locked(table, epoch_column, "INTEGER")
            migrated_rows, invalid_rows = self.store._backfill_epoch_locked(table, time_column, epoch_column)
            migrated += migrated_rows
            invalid += invalid_rows
        return migrated, invalid

    def _create_indexes(self) -> None:
        self.store.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_members_position
                ON conversation_members(conversation_id, position);
            CREATE INDEX IF NOT EXISTS idx_conversation_events_turn
                ON conversation_events(conversation_id, turn_id, id);
            CREATE INDEX IF NOT EXISTS idx_conversation_runtime_traces_turn
                ON conversation_runtime_traces(conversation_id, turn_id, character_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_updated_epoch
                ON conversations(updated_at_epoch DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_conversation_events_time_epoch
                ON conversation_events(conversation_id, event_time_epoch, id);
            """
        )

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store._ensure_migration_table_locked()
            ready = self.store.conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                ("group/002-indexes",),
            ).fetchone()
            if ready is not None:
                return

            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations(
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_members(
                    conversation_id TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, actor_type, actor_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS conversation_runtime_traces(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    source_conversation_event_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    trace_json TEXT NOT NULL,
                    UNIQUE(source_conversation_event_id, character_id)
                );
                """
            )
            self.store.conn.commit()

        self.store.apply_schema_migration("group/001-epoch-time-keys", self._migrate_epoch_keys)
        self.store.apply_schema_migration("group/002-indexes", self._create_indexes)

    @staticmethod
    def _event_from_row(row) -> GroupEvent:
        return GroupEvent(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            event_type=row["event_type"],
            event_time=parse_datetime(row["event_time"]),
            content=row["content"],
            metadata=json.loads(row["metadata_json"]),
        )

    def create_group(self, name: str, member_ids: list[str], now: datetime) -> GroupConversation:
        conversation_id = f"group-{uuid4().hex[:12]}"
        cleaned_name = name.strip() or "新群聊"
        stamp = epoch_us(now)
        with self.store.transaction():
            self.store.conn.execute(
                "INSERT INTO conversations(id,type,name,created_at,created_at_epoch,updated_at,updated_at_epoch) VALUES(?,?,?,?,?,?,?)",
                (conversation_id, "GROUP", cleaned_name, now.isoformat(), stamp, now.isoformat(), stamp),
            )
            self.store.conn.execute(
                "INSERT INTO conversation_members(conversation_id,actor_type,actor_id,position,joined_at,joined_at_epoch) VALUES(?,?,?,?,?,?)",
                (conversation_id, "USER", "user", 0, now.isoformat(), stamp),
            )
            for position, character_id in enumerate(member_ids, start=1):
                self.store.conn.execute(
                    "INSERT INTO conversation_members(conversation_id,actor_type,actor_id,position,joined_at,joined_at_epoch) VALUES(?,?,?,?,?,?)",
                    (conversation_id, "CHARACTER", character_id, position, now.isoformat(), stamp),
                )
        return GroupConversation(
            id=conversation_id,
            name=cleaned_name,
            member_ids=list(member_ids),
            created_at=now,
            updated_at=now,
        )

    def _member_ids(self, conversation_id: str) -> list[str]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT actor_id FROM conversation_members WHERE conversation_id=? AND actor_type='CHARACTER' ORDER BY position",
                (conversation_id,),
            ).fetchall()
        return [str(row["actor_id"]) for row in rows]

    def get_group(self, conversation_id: str) -> GroupConversation | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM conversations WHERE id=? AND type='GROUP'",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return GroupConversation(
            id=row["id"],
            name=row["name"],
            member_ids=self._member_ids(conversation_id),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    def list_groups(self) -> list[GroupConversation]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM conversations WHERE type='GROUP' ORDER BY updated_at_epoch DESC, id DESC"
            ).fetchall()
        return [
            GroupConversation(
                id=row["id"],
                name=row["name"],
                member_ids=self._member_ids(row["id"]),
                created_at=parse_datetime(row["created_at"]),
                updated_at=parse_datetime(row["updated_at"]),
            )
            for row in rows
        ]

    def append_event(self, event: GroupEvent) -> GroupEvent:
        stamp = epoch_us(event.event_time)
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO conversation_events(conversation_id,turn_id,actor_type,actor_id,event_type,event_time,event_time_epoch,content,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event.conversation_id,
                    event.turn_id,
                    event.actor_type,
                    event.actor_id,
                    event.event_type,
                    event.event_time.isoformat(),
                    stamp,
                    event.content,
                    json.dumps(event.metadata, ensure_ascii=False),
                ),
            )
            self.store.conn.execute(
                "UPDATE conversations SET updated_at=?,updated_at_epoch=? WHERE id=?",
                (event.event_time.isoformat(), stamp, event.conversation_id),
            )
            self.store._maybe_commit()
        return event.model_copy(update={"id": cur.lastrowid})

    def list_events(self, conversation_id: str, limit: int = 160) -> list[GroupEvent]:
        limit = max(1, min(int(limit), 500))
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM conversation_events WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def count_user_turns(self, conversation_id: str) -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM conversation_events WHERE conversation_id=? AND actor_type='USER'",
                (conversation_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def add_trace(
        self,
        conversation_id: str,
        turn_id: str,
        character_id: str,
        source_conversation_event_id: int,
        created_at: datetime,
        trace: dict[str, Any],
    ) -> int:
        stamp = epoch_us(created_at)
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO conversation_runtime_traces(conversation_id,turn_id,character_id,source_conversation_event_id,created_at,created_at_epoch,trace_json) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source_conversation_event_id,character_id) DO UPDATE SET created_at=excluded.created_at,created_at_epoch=excluded.created_at_epoch,trace_json=excluded.trace_json",
                (
                    conversation_id,
                    turn_id,
                    character_id,
                    source_conversation_event_id,
                    created_at.isoformat(),
                    stamp,
                    json.dumps(trace, ensure_ascii=False),
                ),
            )
            self.store._maybe_commit()
            return int(cur.lastrowid or 0)

    def list_turn_traces(self, conversation_id: str, turn_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT character_id,source_conversation_event_id,created_at,trace_json FROM conversation_runtime_traces "
                "WHERE conversation_id=? AND turn_id=? ORDER BY id",
                (conversation_id, turn_id),
            ).fetchall()
        result = []
        for row in rows:
            trace = json.loads(row["trace_json"])
            result.append(
                {
                    "character_id": row["character_id"],
                    "source_conversation_event_id": row["source_conversation_event_id"],
                    "created_at": row["created_at"],
                    **trace,
                }
            )
        return result
