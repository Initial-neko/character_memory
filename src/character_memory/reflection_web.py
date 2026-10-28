"""Read-only reflection projection. GET never initializes a model or migrates."""
from __future__ import annotations

from fastapi import HTTPException, Query

from character_memory.application.reflection_service import read_evidence_ref_status
from character_memory.reflection_store import KINDS, ReflectionRepository


class ReflectionReader:
    """Explain past periods from persisted rows, computing reference status now."""

    def __init__(self, store):
        self.store = store

    def _ready(self) -> bool:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('reflection_periods','reflection_watermarks')"
            ).fetchall()
        return len(rows) == 2

    def list(self, character_id: str, *, kind: str | None = None, limit: int = 10) -> dict:
        if kind is not None and kind not in KINDS:
            raise ValueError("invalid reflection kind")
        if not self._ready():
            return {"character_id": character_id, "read_only": True, "periods": []}
        sql = "SELECT * FROM reflection_periods WHERE character_id=?"
        args: list = [character_id]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY window_start DESC,kind LIMIT ?"
        args.append(max(1, min(int(limit), 50)))
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
            forgotten_row = self.store.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE character_id=? AND active=0 AND superseded_by IS NULL",
                (character_id,),
            ).fetchone()
        forgotten = int(forgotten_row[0] or 0) if forgotten_row is not None else 0
        periods = [self._period(row, character_id, forgotten) for row in rows]
        return {"character_id": character_id, "read_only": True, "periods": periods}

    def _period(self, row, character_id: str, forgotten: int) -> dict:
        period = ReflectionRepository._period_payload(row)
        evidence = period.get("evidence") or {}
        refs = []
        unavailable = 0
        for item in evidence.get("facts", []):
            status = read_evidence_ref_status(self.store, character_id, item)
            unavailable += status == "UNAVAILABLE"
            refs.append({
                "kind": item.get("kind"),
                "id": int(item.get("id")),
                "conversation_id": item.get("conversation_id"),
                "status": status,
            })
        for item in evidence.get("memories", []):
            status = read_evidence_ref_status(self.store, character_id, item)
            unavailable += status == "UNAVAILABLE"
            refs.append({
                "kind": "MEMORY",
                "id": int(item.get("memory_id")),
                "memory_id": int(item.get("memory_id")),
                "status": status,
            })
        governance = evidence.get("governance") or {}
        output = period.get("output")
        return {
            "kind": period["kind"],
            "window_start": period["window_start"].isoformat(),
            "window_end": period["window_end"].isoformat(),
            "policy_version": period["policy_version"],
            "status": period["status"],
            "reason": period["reason"],
            "claimed_at": period["claimed_at"].isoformat(),
            "completed_at": period["completed_at"].isoformat() if period["completed_at"] else None,
            "evidence_refs": refs,
            "governance": {
                "active": int(governance.get("active_count", 0)),
                "forgotten": forgotten,
                "superseded": int(governance.get("superseded_count", 0)),
                "unavailable": unavailable,
                "facts_truncated": int(governance.get("facts_truncated", 0)),
                "memories_truncated": int(governance.get("memories_truncated", 0)),
            },
            "output": output,
        }


def attach_reflection_routes(app, access) -> None:
    reader = ReflectionReader(getattr(access, "read_store", None) or access.store())
    ensure_character = getattr(access, "ensure_character", None)

    @app.get("/v1/characters/{character_id}/reflections")
    def reflections(
        character_id: str,
        kind: str | None = Query(default=None, pattern="^(DAILY|WEEKLY|MONTHLY)$"),
        limit: int = Query(default=10, ge=1, le=50),
    ):
        if ensure_character is not None:
            ensure_character(character_id)
        try:
            return reader.list(character_id, kind=kind, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


__all__ = ["ReflectionReader", "attach_reflection_routes"]
