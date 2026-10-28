"""Durable evidence watermarks and period ledger for reflection (P08).

The ledger is deliberately separate from ``world_activity_runs`` and
``space_daily_runs``: reflection owns its own ``(character_id, kind,
window_start)`` identity so it can never be mistaken for a World/Space clock.
Facts, memories and model receipts keep their existing owners.
"""
from __future__ import annotations

import json

from character_memory.time_utils import parse_datetime


KINDS = ("DAILY", "WEEKLY", "MONTHLY")
PERIOD_STATUSES = (
    "RUNNING",
    "COMMITTED",
    "SKIPPED_IDLE",
    "BLOCKED_BUDGET",
    "FAILED",
    "UNKNOWN",
)


class ReflectionRepository:
    MIGRATION = "reflection/001-watermarks-periods"

    def __init__(self, store):
        self.store = store
        store.apply_schema_migration(self.MIGRATION, self._create_schema, immediate=True)

    def _create_schema(self):
        self.store.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reflection_watermarks(
                character_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('DAILY','WEEKLY','MONTHLY')),
                last_finalized_window_end TEXT NOT NULL,
                event_cursor INTEGER NOT NULL,
                conversation_cursor_json TEXT NOT NULL,
                memory_max_id INTEGER NOT NULL,
                memory_signature TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(character_id, kind)
            );
            CREATE TABLE IF NOT EXISTS reflection_periods(
                character_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('DAILY','WEEKLY','MONTHLY')),
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                offset_minutes INTEGER NOT NULL,
                policy_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('RUNNING','COMMITTED','SKIPPED_IDLE','BLOCKED_BUDGET','FAILED','UNKNOWN')),
                reason TEXT NOT NULL,
                evidence_signature TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                output_json TEXT,
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(character_id, kind, window_start)
            );
            CREATE INDEX IF NOT EXISTS idx_reflection_periods_character
                ON reflection_periods(character_id, kind, window_start DESC);
            """
        )

    @staticmethod
    def _watermark_payload(row) -> dict:
        return {
            "character_id": row["character_id"],
            "kind": row["kind"],
            "last_finalized_window_end": parse_datetime(row["last_finalized_window_end"]),
            "event_cursor": int(row["event_cursor"]),
            "conversation_cursors": json.loads(row["conversation_cursor_json"] or "[]"),
            "memory_max_id": int(row["memory_max_id"]),
            "memory_signature": str(row["memory_signature"]),
            "policy_version": int(row["policy_version"]),
            "updated_at": parse_datetime(row["updated_at"]),
        }

    def get_watermark(self, character_id: str, kind: str) -> dict | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM reflection_watermarks WHERE character_id=? AND kind=?",
                (character_id, kind),
            ).fetchone()
        return self._watermark_payload(row) if row is not None else None

    @staticmethod
    def _period_payload(row) -> dict:
        evidence = json.loads(row["evidence_json"] or "{}")
        output = json.loads(row["output_json"]) if row["output_json"] else None
        return {
            "character_id": row["character_id"],
            "kind": row["kind"],
            "window_start": parse_datetime(row["window_start"]),
            "window_end": parse_datetime(row["window_end"]),
            "offset_minutes": int(row["offset_minutes"]),
            "policy_version": int(row["policy_version"]),
            "status": str(row["status"]),
            "reason": str(row["reason"]),
            "evidence_signature": str(row["evidence_signature"]),
            "evidence": evidence,
            "output": output,
            "output_json": row["output_json"],
            "claimed_at": parse_datetime(row["claimed_at"]),
            "completed_at": parse_datetime(row["completed_at"]) if row["completed_at"] else None,
        }

    def get_period(self, character_id: str, kind: str, window_start_iso: str) -> dict | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM reflection_periods "
                "WHERE character_id=? AND kind=? AND window_start=?",
                (character_id, kind, window_start_iso),
            ).fetchone()
        return self._period_payload(row) if row is not None else None

    def claim_period(
        self,
        character_id: str,
        kind: str,
        *,
        window_start: str,
        window_end: str,
        offset_minutes: int,
        policy_version: int,
        evidence_signature: str,
        evidence_json: str,
        claimed_at: str,
    ) -> bool:
        """Insert the one durable claim for this period; False means another writer won.

        The insert is the only concurrency authority. Two processes may compute
        the same evidence, but exactly one row can exist for
        ``(character_id, kind, window_start)``.
        """
        with self.store.transaction(immediate=True):
            cursor = self.store.conn.execute(
                "INSERT OR IGNORE INTO reflection_periods("
                "character_id,kind,window_start,window_end,offset_minutes,policy_version,"
                "status,reason,evidence_signature,evidence_json,claimed_at) "
                "VALUES(?,?,?,?,?,?,'RUNNING','CLAIMED',?,?,?)",
                (
                    character_id,
                    kind,
                    window_start,
                    window_end,
                    int(offset_minutes),
                    int(policy_version),
                    evidence_signature,
                    evidence_json,
                    claimed_at,
                ),
            )
            return cursor.rowcount == 1

    def update_period(
        self,
        character_id: str,
        kind: str,
        window_start: str,
        *,
        status: str,
        reason: str,
        output_json: str | None = None,
        completed_at: str | None = None,
        allowed_statuses: tuple[str, ...] = ("RUNNING",),
    ) -> bool:
        if status not in PERIOD_STATUSES:
            raise ValueError("invalid reflection period status")
        marks = ",".join("?" for _ in allowed_statuses)
        with self.store.transaction(immediate=True):
            cursor = self.store.conn.execute(
                f"UPDATE reflection_periods SET status=?,reason=?,output_json=?,completed_at=? "
                f"WHERE character_id=? AND kind=? AND window_start=? AND status IN ({marks})",
                (
                    status,
                    reason,
                    output_json,
                    completed_at,
                    character_id,
                    kind,
                    window_start,
                    *allowed_statuses,
                ),
            )
            return cursor.rowcount == 1

    def commit_period(
        self,
        character_id: str,
        kind: str,
        *,
        window_start: str,
        reason: str,
        output_json: str,
        completed_at: str,
        window_end: str,
        event_cursor: int,
        conversation_cursors: list[dict],
        memory_max_id: int,
        memory_signature: str,
        policy_version: int,
    ) -> None:
        """Commit the output and advance the watermark in one transaction.

        A period is allowed to move from RUNNING/UNKNOWN to COMMITTED: UNKNOWN
        can only have been written by a competing reader while this writer was
        still working, and the successful receipt is the stronger fact.
        """
        canonical_cursors = json.dumps(
            sorted(conversation_cursors, key=lambda item: item["conversation_id"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.store.transaction(immediate=True):
            updated = self.store.conn.execute(
                "UPDATE reflection_periods SET status='COMMITTED',reason=?,output_json=?,completed_at=? "
                "WHERE character_id=? AND kind=? AND window_start=? "
                "AND status IN ('RUNNING','UNKNOWN','BLOCKED_BUDGET')",
                (reason, output_json, completed_at, character_id, kind, window_start),
            )
            if updated.rowcount != 1:
                raise RuntimeError("REFLECTION_PERIOD_NOT_CLAIMABLE")
            self.store.conn.execute(
                "INSERT INTO reflection_watermarks("
                "character_id,kind,last_finalized_window_end,event_cursor,conversation_cursor_json,"
                "memory_max_id,memory_signature,policy_version,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(character_id,kind) DO UPDATE SET "
                "last_finalized_window_end=excluded.last_finalized_window_end,"
                "event_cursor=excluded.event_cursor,"
                "conversation_cursor_json=excluded.conversation_cursor_json,"
                "memory_max_id=excluded.memory_max_id,"
                "memory_signature=excluded.memory_signature,"
                "policy_version=excluded.policy_version,"
                "updated_at=excluded.updated_at",
                (
                    character_id,
                    kind,
                    window_end,
                    int(event_cursor),
                    canonical_cursors,
                    int(memory_max_id),
                    memory_signature,
                    int(policy_version),
                    completed_at,
                ),
            )

    def list_periods(self, character_id: str, *, kind: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        sql = "SELECT * FROM reflection_periods WHERE character_id=?"
        args: list = [character_id]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY window_start DESC,kind LIMIT ?"
        args.append(limit)
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._period_payload(row) for row in rows]

    def receipt_status(self, character_id: str, opportunity_id: str, step: str) -> str | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT status FROM model_invocation_receipts "
                "WHERE character_id=? AND opportunity_id=? AND step=?",
                (character_id, opportunity_id, step),
            ).fetchone()
        return str(row["status"]) if row is not None else None


__all__ = ["KINDS", "PERIOD_STATUSES", "ReflectionRepository"]
