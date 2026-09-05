from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime
from pathlib import Path

from character_memory.domain.models import Event, EventType, Memory


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
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
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_intents_due ON intents(character_id,status,earliest_at);

            CREATE TABLE IF NOT EXISTS world_states(
                character_id TEXT PRIMARY KEY,
                current_time TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    @staticmethod
    def _pack(v):
        return None if v is None else struct.pack(f"<{len(v)}f", *v)

    @staticmethod
    def _unpack(b):
        return None if b is None else list(struct.unpack(f"<{len(b) // 4}f", b))

    def append_event(self, event: Event) -> Event:
        cur = self.conn.execute(
            "INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?)",
            (
                event.character_id,
                event.event_type.value,
                event.event_time.isoformat(),
                event.content,
                json.dumps(event.metadata, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return event.model_copy(update={"id": cur.lastrowid})

    def add_memory(self, memory: Memory) -> Memory:
        cur = self.conn.execute(
            "INSERT INTO memories(character_id,content,memory_type,event_time,importance,source_event_id,active,metadata_json,embedding) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                memory.character_id,
                memory.content,
                memory.memory_type,
                memory.event_time.isoformat(),
                memory.importance,
                memory.source_event_id,
                int(memory.active),
                json.dumps(memory.metadata, ensure_ascii=False),
                self._pack(memory.embedding),
            ),
        )
        self.conn.commit()
        return memory.model_copy(update={"id": cur.lastrowid})

    def update_memory_embedding(self, memory_id: int, embedding: list[float]):
        self.conn.execute("UPDATE memories SET embedding=? WHERE id=?", (self._pack(embedding), memory_id))
        self.conn.commit()

    def list_memories(self, character_id: str, *, include_inactive: bool = False) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE character_id=?"
        args: list = [character_id]
        if not include_inactive:
            sql += " AND active=1"
        sql += " ORDER BY event_time,id"
        rows = self.conn.execute(sql, args).fetchall()
        return [
            Memory(
                id=r["id"], character_id=r["character_id"], content=r["content"], memory_type=r["memory_type"],
                event_time=datetime.fromisoformat(r["event_time"]), importance=r["importance"], source_event_id=r["source_event_id"],
                active=bool(r["active"]), metadata=json.loads(r["metadata_json"]), embedding=self._unpack(r["embedding"]),
            )
            for r in rows
        ]

    def list_events(self, character_id: str, limit: int = 50, event_type: str | None = None, before: datetime | None = None) -> list[Event]:
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
        return [
            Event(
                id=r["id"], character_id=r["character_id"], event_type=EventType(r["event_type"]),
                event_time=datetime.fromisoformat(r["event_time"]), content=r["content"], metadata=json.loads(r["metadata_json"]),
            )
            for r in rows
        ]

    def get_mental_state(self, character_id: str) -> str:
        r = self.conn.execute("SELECT content FROM mental_states WHERE character_id=?", (character_id,)).fetchone()
        return r["content"] if r else ""

    def set_mental_state(self, character_id: str, content: str, updated_at, source_event_id=None):
        self.conn.execute(
            "INSERT INTO mental_states(character_id,content,updated_at,source_event_id) VALUES(?,?,?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at,source_event_id=excluded.source_event_id",
            (character_id, content, updated_at.isoformat(), source_event_id),
        )
        self.conn.commit()

    def add_intent(self, character_id, content, preferred_action, created_at, earliest_at, expires_at, reason=""):
        cur = self.conn.execute(
            "INSERT INTO intents(character_id,content,preferred_action,created_at,earliest_at,expires_at,status,reason) VALUES(?,?,?,?,?,?,?,?)",
            (character_id, content, preferred_action, created_at.isoformat(), earliest_at.isoformat(), expires_at.isoformat(), "PENDING", reason),
        )
        self.conn.commit()
        return cur.lastrowid

    def expire_intents(self, character_id: str, now: datetime):
        self.conn.execute(
            "UPDATE intents SET status='EXPIRED' WHERE character_id=? AND status='PENDING' AND expires_at<?",
            (character_id, now.isoformat()),
        )
        self.conn.commit()

    def due_intents(self, character_id, now):
        return self.conn.execute(
            "SELECT * FROM intents WHERE character_id=? AND status='PENDING' AND earliest_at<=? AND expires_at>=? ORDER BY earliest_at,id",
            (character_id, now.isoformat(), now.isoformat()),
        ).fetchall()

    def list_intents(self, character_id: str, limit: int = 30):
        return self.conn.execute(
            "SELECT * FROM intents WHERE character_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (character_id, limit)
        ).fetchall()

    def set_intent_status(self, intent_id, status):
        self.conn.execute("UPDATE intents SET status=? WHERE id=?", (status, intent_id))
        self.conn.commit()

    def get_world_time(self, character_id: str) -> datetime | None:
        row = self.conn.execute('SELECT "current_time" FROM world_states WHERE character_id=?', (character_id,)).fetchone()
        return datetime.fromisoformat(row["current_time"]) if row else None

    def set_world_time(self, character_id: str, current_time: datetime):
        self.conn.execute(
            "INSERT INTO world_states(character_id,current_time) VALUES(?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET current_time=excluded.current_time",
            (character_id, current_time.isoformat()),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
