from __future__ import annotations

from dataclasses import dataclass
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
    archived_at: datetime | None = None


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


@dataclass(frozen=True)
class GroupHistoryPage:
    events: list[GroupEvent]
    has_more: bool
    next_before_id: int | None


class GroupRepository:
    """Storage for shared group facts.

    Group events intentionally do not reuse the character-local `events` table.
    The same group fact must exist only once even though several characters may
    observe and remember it differently.

    Archiving is UI lifecycle only: archived conversations disappear from the
    default list but their events/traces/memories/media remain untouched.
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

    def _migrate_archive_state(self) -> None:
        self.store._ensure_column_locked("conversations", "archived_at", "TEXT")
        self.store._ensure_column_locked("conversations", "archived_at_epoch", "INTEGER")
        self.store.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_archive_updated "
            "ON conversations(type,archived_at_epoch,updated_at_epoch DESC,id DESC)"
        )

    def _migrate_autonomy_scheduler(self) -> None:
        self.store.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_autonomy_state(
                conversation_id TEXT PRIMARY KEY,
                last_opportunity_at TEXT,
                last_opportunity_at_epoch INTEGER,
                next_opportunity_at TEXT NOT NULL,
                next_opportunity_at_epoch INTEGER NOT NULL,
                last_status TEXT,
                last_turn_id TEXT,
                updated_at TEXT NOT NULL,
                updated_at_epoch INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_autonomy_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                scheduled_for_epoch INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                started_at_epoch INTEGER NOT NULL,
                completed_at TEXT,
                completed_at_epoch INTEGER,
                status TEXT NOT NULL,
                turn_id TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'SCHEDULED',
                error TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_group_autonomy_state_due
                ON group_autonomy_state(next_opportunity_at_epoch,conversation_id);
            CREATE INDEX IF NOT EXISTS idx_group_autonomy_runs_recent
                ON group_autonomy_runs(conversation_id,started_at_epoch DESC,id DESC);
            """
        )

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store._ensure_migration_table_locked()
            ready = self.store.conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                ("group/002-indexes",),
            ).fetchone()
            if ready is None:
                self.store.conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversations(
                        id TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        archived_at TEXT,
                        archived_at_epoch INTEGER
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
        self.store.apply_schema_migration("group/003-conversation-archive", self._migrate_archive_state)
        self.store.apply_schema_migration("group/004-autonomy-scheduler", self._migrate_autonomy_scheduler)

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

    def _group_from_row(self, row) -> GroupConversation:
        archived_at = row["archived_at"] if "archived_at" in row.keys() else None
        return GroupConversation(
            id=row["id"],
            name=row["name"],
            member_ids=self._member_ids(row["id"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
            archived_at=parse_datetime(archived_at) if archived_at else None,
        )

    def create_group(self, name: str, member_ids: list[str], now: datetime) -> GroupConversation:
        conversation_id = f"group-{uuid4().hex[:12]}"
        cleaned_name = name.strip() or "新群聊"
        stamp = epoch_us(now)
        with self.store.transaction():
            self.store.conn.execute(
                "INSERT INTO conversations(id,type,name,created_at,created_at_epoch,updated_at,updated_at_epoch,archived_at,archived_at_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
                (conversation_id, "GROUP", cleaned_name, now.isoformat(), stamp, now.isoformat(), stamp, None, None),
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
            archived_at=None,
        )

    def rename_group(self, conversation_id: str, name: str, now: datetime) -> GroupConversation | None:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("group name must not be empty")
        stamp = epoch_us(now)
        with self.store._lock:
            cur = self.store.conn.execute(
                "UPDATE conversations SET name=?,updated_at=?,updated_at_epoch=? "
                "WHERE id=? AND type='GROUP' AND archived_at IS NULL",
                (cleaned_name, now.isoformat(), stamp, conversation_id),
            )
            self.store._maybe_commit()
        if cur.rowcount <= 0:
            return None
        return self.get_group(conversation_id)

    def archive_group(self, conversation_id: str, now: datetime) -> GroupConversation | None:
        existing = self.get_group(conversation_id, include_archived=True)
        if existing is None:
            return None
        if existing.archived_at is not None:
            return existing
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE conversations SET archived_at=?,archived_at_epoch=? WHERE id=? AND type='GROUP' AND archived_at IS NULL",
                (now.isoformat(), epoch_us(now), conversation_id),
            )
            self.store._maybe_commit()
        return self.get_group(conversation_id, include_archived=True)

    def restore_group(self, conversation_id: str) -> GroupConversation | None:
        existing = self.get_group(conversation_id, include_archived=True)
        if existing is None:
            return None
        if existing.archived_at is None:
            return existing
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE conversations SET archived_at=NULL,archived_at_epoch=NULL WHERE id=? AND type='GROUP'",
                (conversation_id,),
            )
            self.store._maybe_commit()
        return self.get_group(conversation_id)

    def _member_ids(self, conversation_id: str) -> list[str]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT actor_id FROM conversation_members WHERE conversation_id=? AND actor_type='CHARACTER' ORDER BY position",
                (conversation_id,),
            ).fetchall()
        return [str(row["actor_id"]) for row in rows]

    def get_group(self, conversation_id: str, *, include_archived: bool = False) -> GroupConversation | None:
        with self.store._lock:
            sql = "SELECT * FROM conversations WHERE id=? AND type='GROUP'"
            if not include_archived:
                sql += " AND archived_at IS NULL"
            row = self.store.conn.execute(sql, (conversation_id,)).fetchone()
        if row is None:
            return None
        return self._group_from_row(row)

    def list_groups(self, *, archived: bool = False) -> list[GroupConversation]:
        with self.store._lock:
            if archived:
                rows = self.store.conn.execute(
                    "SELECT * FROM conversations WHERE type='GROUP' AND archived_at IS NOT NULL "
                    "ORDER BY archived_at_epoch DESC,id DESC"
                ).fetchall()
            else:
                rows = self.store.conn.execute(
                    "SELECT * FROM conversations WHERE type='GROUP' AND archived_at IS NULL "
                    "ORDER BY updated_at_epoch DESC,id DESC"
                ).fetchall()
        return [self._group_from_row(row) for row in rows]

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
            if not bool((event.metadata or {}).get("hidden")):
                self.store.conn.execute(
                    "UPDATE conversations SET updated_at=?,updated_at_epoch=? WHERE id=?",
                    (event.event_time.isoformat(), stamp, event.conversation_id),
                )
            self.store._maybe_commit()
        return event.model_copy(update={"id": cur.lastrowid})

    def update_event_metadata(self, event_id: int, metadata: dict[str, Any]) -> bool:
        """Replace a conversation event's metadata document; see SQLiteStore."""
        with self.store._lock:
            cur = self.store.conn.execute(
                "UPDATE conversation_events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), event_id),
            )
            self.store._maybe_commit()
            return cur.rowcount > 0

    def list_events(self, conversation_id: str, limit: int = 160) -> list[GroupEvent]:
        limit = max(1, min(int(limit), 500))
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM conversation_events WHERE conversation_id=? AND event_time_epoch IS NOT NULL ORDER BY event_time_epoch DESC,id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def list_event_page(self, conversation_id: str, *, limit: int = 50, before_id: int | None = None) -> GroupHistoryPage:
        page_size = max(1, min(int(limit), 100))
        with self.store._lock:
            sql = "SELECT * FROM conversation_events WHERE conversation_id=? AND event_time_epoch IS NOT NULL AND actor_type IN ('USER','CHARACTER')"
            args: list = [conversation_id]
            if before_id is not None:
                cursor = self.store.conn.execute(
                    "SELECT event_time_epoch,id FROM conversation_events WHERE id=? AND conversation_id=?",
                    (int(before_id), conversation_id),
                ).fetchone()
                if cursor is None or cursor["event_time_epoch"] is None:
                    return GroupHistoryPage([], False, None)
                sql += " AND (event_time_epoch<? OR (event_time_epoch=? AND id<?))"
                args.extend([cursor["event_time_epoch"], cursor["event_time_epoch"], cursor["id"]])
            sql += " ORDER BY event_time_epoch DESC,id DESC LIMIT ?"
            args.append(page_size + 1)
            rows = self.store.conn.execute(sql, args).fetchall()

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        events = [self._event_from_row(row) for row in reversed(rows)]
        next_before_id = int(events[0].id) if has_more and events else None
        return GroupHistoryPage(events, has_more, next_before_id)

    def list_turn_events(self, conversation_id: str, turn_id: str) -> list[GroupEvent]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM conversation_events WHERE conversation_id=? AND turn_id=? AND event_time_epoch IS NOT NULL ORDER BY id",
                (conversation_id, turn_id),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_autonomy_state(self, conversation_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM group_autonomy_state WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def ensure_autonomy_state(
        self,
        conversation_id: str,
        now: datetime,
        interval_minutes: float,
    ) -> dict[str, Any]:
        interval_seconds = max(600.0, float(interval_minutes) * 60.0)
        next_at = datetime.fromtimestamp(now.timestamp() + interval_seconds, tz=now.tzinfo)
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO group_autonomy_state("
                "conversation_id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch"
                ") VALUES(?,?,?,?,?)",
                (
                    conversation_id,
                    next_at.isoformat(),
                    epoch_us(next_at),
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM group_autonomy_state WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row)

    def set_next_autonomy_opportunity(
        self,
        conversation_id: str,
        next_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO group_autonomy_state("
                "conversation_id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "next_opportunity_at=excluded.next_opportunity_at,"
                "next_opportunity_at_epoch=excluded.next_opportunity_at_epoch,"
                "updated_at=excluded.updated_at,updated_at_epoch=excluded.updated_at_epoch",
                (
                    conversation_id,
                    next_at.isoformat(),
                    epoch_us(next_at),
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM group_autonomy_state WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row)

    def claim_due_autonomy_opportunity(
        self,
        conversation_id: str,
        now: datetime,
        interval_minutes: float,
        *,
        source: str = "SCHEDULED",
    ) -> int | None:
        interval_seconds = max(600.0, float(interval_minutes) * 60.0)
        next_at = datetime.fromtimestamp(now.timestamp() + interval_seconds, tz=now.tzinfo)
        now_epoch = epoch_us(now)
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM group_autonomy_state WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if row is None or int(row["next_opportunity_at_epoch"]) > now_epoch:
                return None
            cur = self.store.conn.execute(
                "INSERT INTO group_autonomy_runs("
                "conversation_id,scheduled_for,scheduled_for_epoch,started_at,started_at_epoch,status,source,error"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    conversation_id,
                    str(row["next_opportunity_at"]),
                    int(row["next_opportunity_at_epoch"]),
                    now.isoformat(),
                    now_epoch,
                    "RUNNING",
                    str(source or "SCHEDULED"),
                    "",
                ),
            )
            self.store.conn.execute(
                "UPDATE group_autonomy_state SET "
                "next_opportunity_at=?,next_opportunity_at_epoch=?,updated_at=?,updated_at_epoch=? "
                "WHERE conversation_id=?",
                (
                    next_at.isoformat(),
                    epoch_us(next_at),
                    now.isoformat(),
                    now_epoch,
                    conversation_id,
                ),
            )
            self.store._maybe_commit()
            return int(cur.lastrowid)

    def finish_autonomy_run(
        self,
        run_id: int,
        conversation_id: str,
        now: datetime,
        *,
        status: str,
        turn_id: str | None = None,
        message_count: int = 0,
        error: str = "",
    ) -> None:
        normalized = str(status or "").strip().upper()
        if normalized not in {"CHATTED", "NO_CHAT", "SKIPPED", "FAILED", "SUPERSEDED"}:
            raise ValueError("invalid Group autonomy run status")
        now_epoch = epoch_us(now)
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE group_autonomy_runs SET completed_at=?,completed_at_epoch=?,status=?,turn_id=?,message_count=?,error=? "
                "WHERE id=? AND conversation_id=?",
                (
                    now.isoformat(),
                    now_epoch,
                    normalized,
                    turn_id,
                    max(0, int(message_count)),
                    str(error or "")[:2000],
                    int(run_id),
                    conversation_id,
                ),
            )
            self.store.conn.execute(
                "UPDATE group_autonomy_state SET "
                "last_opportunity_at=?,last_opportunity_at_epoch=?,last_status=?,last_turn_id=?,"
                "updated_at=?,updated_at_epoch=? WHERE conversation_id=?",
                (
                    now.isoformat(),
                    now_epoch,
                    normalized,
                    turn_id,
                    now.isoformat(),
                    now_epoch,
                    conversation_id,
                ),
            )
            self.store._maybe_commit()

    def list_autonomy_runs(
        self,
        *,
        conversation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM group_autonomy_runs"
        args: list[Any] = []
        if conversation_id:
            sql += " WHERE conversation_id=?"
            args.append(conversation_id)
        sql += " ORDER BY started_at_epoch DESC,id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def latest_user_event(self, conversation_id: str) -> GroupEvent | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM conversation_events WHERE conversation_id=? AND actor_type='USER' "
                "AND event_time_epoch IS NOT NULL ORDER BY event_time_epoch DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

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

    def turn_summaries(self, conversation_id: str, turn_ids: list[str]) -> dict[str, dict[str, int]]:
        ids = list(dict.fromkeys(str(value) for value in turn_ids if str(value or "").strip()))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.store._lock:
            rows = self.store.conn.execute(
                f"SELECT turn_id,trace_json FROM conversation_runtime_traces WHERE conversation_id=? AND turn_id IN ({placeholders}) ORDER BY id",
                [conversation_id, *ids],
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            summary = result.setdefault(str(row["turn_id"]), {"total": 0, "replied": 0, "silent": 0})
            summary["total"] += 1
            try:
                trace = json.loads(row["trace_json"])
            except (TypeError, ValueError):
                trace = {}
            if trace.get("actions"):
                summary["replied"] += 1
            else:
                summary["silent"] += 1
        return result
