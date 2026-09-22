from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

from character_memory.time_utils import epoch_us, parse_datetime


ENCOUNTER_SOURCE_TYPES = {"WEB", "GENERATED"}
ENCOUNTER_STATUSES = {"NEW", "SEEN", "CHATTING", "ACCEPTED", "DISMISSED", "EXPIRED", "FAILED"}


class EncounterRepository:
    """Durable candidate pool kept separate from formal character personas."""

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        now = datetime.now().astimezone()
        with self.store._lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS encounter_candidates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    encounter_hook TEXT NOT NULL DEFAULT '',
                    opening_message TEXT NOT NULL DEFAULT '',
                    source_query TEXT NOT NULL DEFAULT '',
                    source_urls_json TEXT NOT NULL DEFAULT '[]',
                    source_domains_json TEXT NOT NULL DEFAULT '[]',
                    fingerprint TEXT NOT NULL,
                    accepted_character_id TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_encounter_candidates_fingerprint
                    ON encounter_candidates(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_encounter_candidates_status_created
                    ON encounter_candidates(status,created_at_epoch DESC,id DESC);

                CREATE TABLE IF NOT EXISTS encounter_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_encounter_messages_candidate
                    ON encounter_messages(candidate_id,created_at_epoch,id);

                CREATE TABLE IF NOT EXISTS encounter_state(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    last_opportunity_at TEXT,
                    last_opportunity_at_epoch INTEGER,
                    next_opportunity_at TEXT NOT NULL,
                    next_opportunity_at_epoch INTEGER NOT NULL,
                    last_status TEXT,
                    last_candidate_id INTEGER,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS encounter_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheduled_for TEXT NOT NULL,
                    scheduled_for_epoch INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    started_at_epoch INTEGER NOT NULL,
                    completed_at TEXT,
                    completed_at_epoch INTEGER,
                    source_type TEXT,
                    status TEXT NOT NULL,
                    candidate_id INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_encounter_runs_started
                    ON encounter_runs(started_at_epoch DESC,id DESC);
                """
            )
            self.store._ensure_migration_table_locked()
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("encounter/001-core", now.isoformat()),
            )
            self.store._maybe_commit()

    @staticmethod
    def _loads(raw: str, default):
        try:
            value = json.loads(raw or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value

    def _candidate(self, row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "source_type": str(row["source_type"]),
            "status": str(row["status"]),
            "draft": self._loads(row["draft_json"], {}),
            "encounter_hook": str(row["encounter_hook"] or ""),
            "opening_message": str(row["opening_message"] or ""),
            "source_query": str(row["source_query"] or ""),
            "source_urls": self._loads(row["source_urls_json"], []),
            "source_domains": self._loads(row["source_domains_json"], []),
            "accepted_character_id": row["accepted_character_id"],
            "error": str(row["error"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def fingerprint(draft: dict[str, Any], source_type: str, source_urls: list[str]) -> str:
        identity = " ".join(
            str(draft.get(key) or "").strip().casefold()
            for key in ("name", "identity", "tagline", "description")
        )
        material = f"{source_type}|{identity}|{'|'.join(source_urls[:3])}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def create_candidate(
        self,
        *,
        source_type: str,
        draft: dict[str, Any],
        encounter_hook: str,
        opening_message: str,
        now: datetime,
        source_query: str = "",
        source_urls: list[str] | None = None,
        source_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        source = str(source_type or "").strip().upper()
        if source not in ENCOUNTER_SOURCE_TYPES:
            raise ValueError("invalid encounter source type")
        urls = [str(item).strip() for item in (source_urls or []) if str(item).strip()][:8]
        domains = [str(item).strip() for item in (source_domains or []) if str(item).strip()][:8]
        fingerprint = self.fingerprint(draft, source, urls)
        stamp = epoch_us(now)
        with self.store._lock:
            existing = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                return self._candidate(existing)
            cur = self.store.conn.execute(
                """
                INSERT INTO encounter_candidates(
                    source_type,status,draft_json,encounter_hook,opening_message,
                    source_query,source_urls_json,source_domains_json,fingerprint,
                    created_at,created_at_epoch,updated_at,updated_at_epoch
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source,
                    "NEW",
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    str(encounter_hook or "").strip()[:1200],
                    str(opening_message or "").strip()[:1200],
                    str(source_query or "").strip()[:500],
                    json.dumps(urls, ensure_ascii=False),
                    json.dumps(domains, ensure_ascii=False),
                    fingerprint,
                    now.isoformat(),
                    stamp,
                    now.isoformat(),
                    stamp,
                ),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE id=?",
                (int(cur.lastrowid),),
            ).fetchone()
        return self._candidate(row)

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE id=?",
                (int(candidate_id),),
            ).fetchone()
        return self._candidate(row) if row is not None else None

    def list_candidates(self, *, limit: int = 20, include_closed: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM encounter_candidates"
        args: list[Any] = []
        if not include_closed:
            sql += " WHERE status IN ('NEW','SEEN','CHATTING')"
        sql += " ORDER BY created_at_epoch DESC,id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 100)))
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._candidate(row) for row in rows]

    def pending_count(self) -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM encounter_candidates WHERE status IN ('NEW','SEEN','CHATTING')"
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def set_status(
        self,
        candidate_id: int,
        status: str,
        now: datetime,
        *,
        accepted_character_id: str | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        normalized = str(status or "").strip().upper()
        if normalized not in ENCOUNTER_STATUSES:
            raise ValueError("invalid encounter status")
        with self.store._lock:
            self.store.conn.execute(
                """
                UPDATE encounter_candidates
                SET status=?,accepted_character_id=COALESCE(?,accepted_character_id),
                    error=?,updated_at=?,updated_at_epoch=?
                WHERE id=?
                """,
                (
                    normalized,
                    accepted_character_id,
                    str(error or "")[:2000],
                    now.isoformat(),
                    epoch_us(now),
                    int(candidate_id),
                ),
            )
            self.store._maybe_commit()
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError("encounter candidate not found")
        return candidate

    def append_message(self, candidate_id: int, role: str, content: str, now: datetime) -> dict[str, Any]:
        if self.get_candidate(candidate_id) is None:
            raise KeyError("encounter candidate not found")
        normalized_role = str(role or "").strip().upper()
        if normalized_role not in {"USER", "CHARACTER"}:
            raise ValueError("invalid encounter message role")
        text = str(content or "").strip()
        if not text:
            raise ValueError("encounter message must not be empty")
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO encounter_messages(candidate_id,role,content,created_at,created_at_epoch) VALUES(?,?,?,?,?)",
                (int(candidate_id), normalized_role, text[:8000], now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
        return {
            "id": int(cur.lastrowid),
            "candidate_id": int(candidate_id),
            "role": normalized_role,
            "content": text[:8000],
            "created_at": now.isoformat(),
        }

    def list_messages(self, candidate_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM encounter_messages WHERE candidate_id=? ORDER BY created_at_epoch,id LIMIT ?",
                (int(candidate_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "candidate_id": int(row["candidate_id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def ensure_state(self, now: datetime, interval_minutes: float) -> dict[str, Any]:
        next_at = datetime.fromtimestamp(
            now.timestamp() + max(600.0, float(interval_minutes) * 60.0),
            tz=now.tzinfo,
        )
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO encounter_state(id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch) VALUES(1,?,?,?,?)",
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute("SELECT * FROM encounter_state WHERE id=1").fetchone()
        return dict(row)

    def set_next_opportunity(self, next_at: datetime, now: datetime) -> dict[str, Any]:
        with self.store._lock:
            self.store.conn.execute(
                """
                INSERT INTO encounter_state(id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch)
                VALUES(1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    next_opportunity_at=excluded.next_opportunity_at,
                    next_opportunity_at_epoch=excluded.next_opportunity_at_epoch,
                    updated_at=excluded.updated_at,updated_at_epoch=excluded.updated_at_epoch
                """,
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute("SELECT * FROM encounter_state WHERE id=1").fetchone()
        return dict(row)

    def claim_due(self, now: datetime, interval_minutes: float) -> int | None:
        state = self.ensure_state(now, interval_minutes)
        if int(state["next_opportunity_at_epoch"]) > epoch_us(now):
            return None
        next_at = datetime.fromtimestamp(
            now.timestamp() + max(600.0, float(interval_minutes) * 60.0),
            tz=now.tzinfo,
        )
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO encounter_runs(scheduled_for,scheduled_for_epoch,started_at,started_at_epoch,status) VALUES(?,?,?,?,?)",
                (
                    str(state["next_opportunity_at"]),
                    int(state["next_opportunity_at_epoch"]),
                    now.isoformat(),
                    epoch_us(now),
                    "RUNNING",
                ),
            )
            self.store.conn.execute(
                "UPDATE encounter_state SET next_opportunity_at=?,next_opportunity_at_epoch=?,updated_at=?,updated_at_epoch=? WHERE id=1",
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        now: datetime,
        *,
        status: str,
        source_type: str | None = None,
        candidate_id: int | None = None,
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(status or "").strip().upper()
        if normalized not in {"CREATED", "SKIPPED_PENDING", "FAILED"}:
            raise ValueError("invalid encounter run status")
        with self.store._lock:
            self.store.conn.execute(
                """
                UPDATE encounter_runs SET completed_at=?,completed_at_epoch=?,source_type=?,status=?,
                    candidate_id=?,error=?,details_json=? WHERE id=?
                """,
                (
                    now.isoformat(),
                    epoch_us(now),
                    source_type,
                    normalized,
                    candidate_id,
                    str(error or "")[:2000],
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    int(run_id),
                ),
            )
            self.store.conn.execute(
                """
                UPDATE encounter_state SET last_opportunity_at=?,last_opportunity_at_epoch=?,
                    last_status=?,last_candidate_id=?,updated_at=?,updated_at_epoch=? WHERE id=1
                """,
                (
                    now.isoformat(),
                    epoch_us(now),
                    normalized,
                    candidate_id,
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()

    def list_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM encounter_runs ORDER BY started_at_epoch DESC,id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["details"] = self._loads(item.pop("details_json", "{}"), {})
            items.append(item)
        return items
