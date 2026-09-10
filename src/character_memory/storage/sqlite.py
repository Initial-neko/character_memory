from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import sqlite3
import struct
import threading
from datetime import datetime
from pathlib import Path

from character_memory.domain.models import Event, EventType, Memory
from character_memory.media import MediaAsset


logger = logging.getLogger("character_memory.storage")


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_character_time ON events(character_id,event_time);
                CREATE INDEX IF NOT EXISTS idx_events_character_type_time ON events(character_id,event_type,event_time);

                CREATE TABLE IF NOT EXISTS memories(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    importance REAL NOT NULL,
                    source_event_id INTEGER,
                    active INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    embedding BLOB
                );
                CREATE INDEX IF NOT EXISTS idx_memories_character_active ON memories(character_id,active);
                CREATE INDEX IF NOT EXISTS idx_memories_character_time ON memories(character_id,event_time);

                CREATE TABLE IF NOT EXISTS mental_states(
                    character_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_event_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS intents(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    preferred_action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    earliest_at TEXT,
                    expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    reason TEXT NOT NULL DEFAULT '',
                    source_event_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_intents_due ON intents(character_id,status,earliest_at);

                CREATE TABLE IF NOT EXISTS media_assets(
                    id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    storage_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_assets_character_time ON media_assets(character_id,created_at);

                CREATE TABLE IF NOT EXISTS world_states(
                    character_id TEXT PRIMARY KEY,
                    current_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_traces(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    trace_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_traces_character_source
                    ON runtime_traces(character_id,source_event_id);
                """
            )
            intent_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(intents)").fetchall()}
            if "source_event_id" not in intent_columns:
                self.conn.execute("ALTER TABLE intents ADD COLUMN source_event_id INTEGER")
                logger.info("storage.intent_migration added=source_event_id")
            migrated = self._migrate_legacy_action_traces_locked()
            self.conn.commit()
            if migrated:
                logger.info("storage.trace_migration migrated=%d", migrated)

    def _migrate_legacy_action_traces_locked(self) -> int:
        rows = self.conn.execute(
            "SELECT id,character_id,event_time,metadata_json FROM events "
            "WHERE event_type=? AND metadata_json LIKE '%\"trace\"%'",
            (EventType.ACTION.value,),
        ).fetchall()
        migrated = 0
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError):
                continue
            trace = metadata.pop("trace", None)
            if not isinstance(trace, dict):
                continue
            source_event_id = trace.get("source_event_id") or metadata.get("source_event_id")
            if source_event_id is None:
                continue
            self.conn.execute(
                "INSERT INTO runtime_traces(character_id,source_event_id,created_at,trace_json) "
                "VALUES(?,?,?,?) ON CONFLICT(source_event_id) DO NOTHING",
                (row["character_id"], int(source_event_id), row["event_time"], json.dumps(trace, ensure_ascii=False)),
            )
            self.conn.execute(
                "UPDATE events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), row["id"]),
            )
            migrated += 1
        return migrated

    def _maybe_commit(self):
        if self._tx_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self.conn.execute("BEGIN")
            self._tx_depth += 1
            try:
                yield
            except Exception:
                self._tx_depth -= 1
                if outer:
                    self.conn.rollback()
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self.conn.commit()

    @staticmethod
    def _pack(v):
        return None if v is None else struct.pack(f"<{len(v)}f", *v)

    @staticmethod
    def _unpack(b):
        return None if b is None else list(struct.unpack(f"<{len(b) // 4}f", b))

    @staticmethod
    def _event_from_row(r) -> Event:
        return Event(id=r["id"], character_id=r["character_id"], event_type=EventType(r["event_type"]), event_time=datetime.fromisoformat(r["event_time"]), content=r["content"], metadata=json.loads(r["metadata_json"]))

    @staticmethod
    def _media_from_row(r) -> MediaAsset:
        return MediaAsset(
            id=r["id"],
            character_id=r["character_id"],
            source=r["source"],
            original_name=r["original_name"],
            mime_type=r["mime_type"],
            storage_name=r["storage_name"],
            created_at=datetime.fromisoformat(r["created_at"]),
            size_bytes=r["size_bytes"],
        )

    def append_event(self, event: Event) -> Event:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?)",
                (event.character_id, event.event_type.value, event.event_time.isoformat(), event.content, json.dumps(event.metadata, ensure_ascii=False)),
            )
            self._maybe_commit()
            return event.model_copy(update={"id": cur.lastrowid})

    def add_media_asset(self, asset: MediaAsset) -> MediaAsset:
        with self._lock:
            self.conn.execute(
                "INSERT INTO media_assets(id,character_id,source,original_name,mime_type,storage_name,created_at,size_bytes) VALUES(?,?,?,?,?,?,?,?)",
                (asset.id, asset.character_id, asset.source, asset.original_name, asset.mime_type, asset.storage_name, asset.created_at.isoformat(), asset.size_bytes),
            )
            self._maybe_commit()
            return asset

    def get_media_asset(self, media_id: str) -> MediaAsset | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
            return self._media_from_row(row) if row else None

    def delete_media_asset(self, media_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM media_assets WHERE id=?", (media_id,))
            self._maybe_commit()

    def add_memory(self, memory: Memory) -> Memory:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO memories(character_id,content,memory_type,event_time,importance,source_event_id,active,metadata_json,embedding) VALUES(?,?,?,?,?,?,?,?,?)",
                (memory.character_id, memory.content, memory.memory_type, memory.event_time.isoformat(), memory.importance, memory.source_event_id, int(memory.active), json.dumps(memory.metadata, ensure_ascii=False), self._pack(memory.embedding)),
            )
            self._maybe_commit()
            return memory.model_copy(update={"id": cur.lastrowid})

    def update_memory_embedding(self, memory_id: int, embedding: list[float]):
        with self._lock:
            self.conn.execute("UPDATE memories SET embedding=? WHERE id=?", (self._pack(embedding), memory_id))
            self._maybe_commit()

    def list_memories(self, character_id: str, *, include_inactive: bool = False, limit: int | None = None, include_embedding: bool = True) -> list[Memory]:
        with self._lock:
            sql = "SELECT * FROM memories WHERE character_id=?"
            args: list = [character_id]
            if not include_inactive:
                sql += " AND active=1"
            if limit is None:
                sql += " ORDER BY event_time,id"
                rows = self.conn.execute(sql, args).fetchall()
            else:
                sql += " ORDER BY event_time DESC,id DESC LIMIT ?"
                rows = list(reversed(self.conn.execute(sql, [*args, limit]).fetchall()))
            return [Memory(id=r["id"], character_id=r["character_id"], content=r["content"], memory_type=r["memory_type"], event_time=datetime.fromisoformat(r["event_time"]), importance=r["importance"], source_event_id=r["source_event_id"], active=bool(r["active"]), metadata=json.loads(r["metadata_json"]), embedding=self._unpack(r["embedding"]) if include_embedding else None) for r in rows]

    def list_events(self, character_id: str, limit: int = 50, event_type: str | None = None, before: datetime | None = None) -> list[Event]:
        with self._lock:
            sql = "SELECT * FROM events WHERE character_id=?"
            args: list = [character_id]
            if event_type:
                sql += " AND event_type=?"
                args.append(event_type)
            if before:
                sql += " AND event_time<=?"
                args.append(before.isoformat())
            sql += " ORDER BY event_time DESC,id DESC LIMIT ?"
            args.append(limit)
            rows = list(reversed(self.conn.execute(sql, args).fetchall()))
            return [self._event_from_row(r) for r in rows]

    def list_chat_events(self, character_id: str, limit: int = 160) -> list[Event]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE character_id=? AND event_type IN (?,?) ORDER BY event_time DESC,id DESC LIMIT ?",
                (character_id, EventType.USER_MESSAGE.value, EventType.CHARACTER_MESSAGE.value, limit),
            ).fetchall()
            return [self._event_from_row(r) for r in reversed(rows)]

    def add_runtime_trace(self, character_id: str, source_event_id: int, created_at: datetime, trace: dict) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT INTO runtime_traces(character_id,source_event_id,created_at,trace_json) VALUES(?,?,?,?) ON CONFLICT(source_event_id) DO UPDATE SET character_id=excluded.character_id,created_at=excluded.created_at,trace_json=excluded.trace_json",
                (character_id, source_event_id, created_at.isoformat(), json.dumps(trace, ensure_ascii=False)),
            )
            row = self.conn.execute("SELECT id FROM runtime_traces WHERE source_event_id=?", (source_event_id,)).fetchone()
            self._maybe_commit()
            return int(row["id"])

    def get_runtime_trace(self, source_event_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT trace_json FROM runtime_traces WHERE source_event_id=?", (source_event_id,)).fetchone()
            return json.loads(row["trace_json"]) if row else None

    def list_runtime_trace_sources(self, character_id: str) -> set[int]:
        with self._lock:
            rows = self.conn.execute("SELECT source_event_id FROM runtime_traces WHERE character_id=?", (character_id,)).fetchall()
            return {int(row["source_event_id"]) for row in rows}

    def get_mental_state(self, character_id: str) -> str:
        with self._lock:
            r = self.conn.execute("SELECT content FROM mental_states WHERE character_id=?", (character_id,)).fetchone()
            return r["content"] if r else ""

    def set_mental_state(self, character_id: str, content: str, updated_at, source_event_id=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO mental_states(character_id,content,updated_at,source_event_id) VALUES(?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at,source_event_id=excluded.source_event_id",
                (character_id, content, updated_at.isoformat(), source_event_id),
            )
            self._maybe_commit()

    def add_intent(self, character_id, content, preferred_action, created_at, earliest_at, expires_at, reason="", *, source_event_id=None):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO intents(character_id,content,preferred_action,created_at,earliest_at,expires_at,status,reason,source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (character_id, content, preferred_action, created_at.isoformat(), earliest_at.isoformat(), expires_at.isoformat(), "PENDING", reason, source_event_id),
            )
            self._maybe_commit()
            return cur.lastrowid

    def expire_intents(self, character_id: str, now: datetime):
        with self._lock:
            self.conn.execute("UPDATE intents SET status='EXPIRED' WHERE character_id=? AND status='PENDING' AND expires_at<?", (character_id, now.isoformat()))
            self._maybe_commit()

    def due_intents(self, character_id, now):
        with self._lock:
            return self.conn.execute("SELECT * FROM intents WHERE character_id=? AND status='PENDING' AND earliest_at<=? AND expires_at>=? ORDER BY earliest_at,id", (character_id, now.isoformat(), now.isoformat())).fetchall()

    def list_intents(self, character_id: str, limit: int = 30):
        with self._lock:
            return self.conn.execute("SELECT * FROM intents WHERE character_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (character_id, limit)).fetchall()

    def set_intent_status(self, intent_id, status):
        with self._lock:
            self.conn.execute("UPDATE intents SET status=? WHERE id=?", (status, intent_id))
            self._maybe_commit()

    def get_world_time(self, character_id: str) -> datetime | None:
        with self._lock:
            row = self.conn.execute('SELECT "current_time" FROM world_states WHERE character_id=?', (character_id,)).fetchone()
            return datetime.fromisoformat(row["current_time"]) if row else None

    def set_world_time(self, character_id: str, current_time: datetime):
        with self._lock:
            self.conn.execute("INSERT INTO world_states(character_id,current_time) VALUES(?,?) ON CONFLICT(character_id) DO UPDATE SET current_time=excluded.current_time", (character_id, current_time.isoformat()))
            self._maybe_commit()

    def close(self):
        with self._lock:
            self.conn.close()
