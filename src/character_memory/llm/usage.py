from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


@dataclass(frozen=True)
class LlmUsageContext:
    feature: str = ""
    purpose: str = ""
    character_id: str = ""
    conversation_id: str = ""
    logical_call_id: str = ""
    attempt: int = 0


_CURRENT_USAGE: ContextVar[LlmUsageContext] = ContextVar(
    "character_memory_llm_usage",
    default=LlmUsageContext(),
)


def current_llm_usage_context() -> LlmUsageContext:
    return _CURRENT_USAGE.get()


@contextmanager
def llm_usage_scope(
    *,
    feature: str | None = None,
    purpose: str | None = None,
    character_id: str | None = None,
    conversation_id: str | None = None,
    logical_call_id: str | None = None,
    attempt: int | None = None,
    override: bool = False,
) -> Iterator[LlmUsageContext]:
    """Attach product-level attribution to every provider request in the block.

    Nested generic callers (for example PersonaBuilder) should normally keep an
    outer feature/purpose chosen by a more specific workflow (for example
    Ensemble). Set override=True only at a boundary that knows better.
    """

    base = current_llm_usage_context()

    def pick(old: str, new: str | None) -> str:
        clean = str(new or "").strip()
        if not clean:
            return old
        if override or not old:
            return clean
        return old

    merged = replace(
        base,
        feature=pick(base.feature, feature),
        purpose=pick(base.purpose, purpose),
        character_id=pick(base.character_id, character_id),
        conversation_id=pick(base.conversation_id, conversation_id),
        logical_call_id=pick(base.logical_call_id, logical_call_id),
        attempt=max(0, int(attempt)) if attempt is not None else base.attempt,
    )
    token = _CURRENT_USAGE.set(merged)
    try:
        yield merged
    finally:
        _CURRENT_USAGE.reset(token)


def new_logical_call_id(prefix: str = "llm") -> str:
    clean = "".join(ch for ch in str(prefix or "llm").lower() if ch.isalnum() or ch in "-_")[:24] or "llm"
    return f"{clean}-{uuid.uuid4().hex[:16]}"


def infer_usage_context(conversation_id: str | None) -> LlmUsageContext:
    value = str(conversation_id or "").strip()
    lower = value.lower()
    if lower.startswith("group:"):
        return LlmUsageContext("GROUP", "GROUP_REACTION", conversation_id=value)
    if lower.startswith("space:"):
        purpose = "SPACE_REPLY" if ":thread:" in lower else "SPACE_AUDIENCE"
        return LlmUsageContext("SPACE", purpose, conversation_id=value)
    if lower.startswith("space-opportunity:"):
        return LlmUsageContext("SPACE", "SPACE_POST_PLAN", conversation_id=value)
    if lower.startswith("space-world-explore:"):
        return LlmUsageContext("SPACE", "SPACE_WORLD_EXPLORE", conversation_id=value)
    if lower.startswith("space-world-appraise:"):
        return LlmUsageContext("SPACE", "SPACE_WORLD_APPRAISAL", conversation_id=value)
    if lower.startswith("avatar-intent:"):
        character_id = value.split(":", 1)[1] if ":" in value else ""
        return LlmUsageContext("AVATAR", "AVATAR_SEARCH_INTENT", character_id=character_id, conversation_id=value)
    if lower.startswith("visual-plan:"):
        parts = value.split(":")
        character_id = parts[1] if len(parts) > 1 else ""
        return LlmUsageContext("VISUAL", "VISUAL_PROMPT", character_id=character_id, conversation_id=value)
    if lower.startswith("sticker-tag:"):
        return LlmUsageContext("STICKER", "STICKER_AUTO_TAG", conversation_id=value)
    if lower == "persona-builder":
        return LlmUsageContext("PERSONA", "PERSONA_BUILD", conversation_id=value)
    if lower.startswith("ensemble-research:"):
        return LlmUsageContext("ENSEMBLE", "ENSEMBLE_RESEARCH", conversation_id=value)
    if lower.startswith("encounter-web-seed:"):
        return LlmUsageContext("ENCOUNTER", "ENCOUNTER_WEB_SEED", conversation_id=value)
    if lower.startswith("encounter-presentation:"):
        return LlmUsageContext("ENCOUNTER", "ENCOUNTER_PRESENTATION", conversation_id=value)
    if lower.startswith("encounter-chat:"):
        return LlmUsageContext("ENCOUNTER", "ENCOUNTER_CHAT", conversation_id=value)
    if lower.startswith("dev-console"):
        purpose = "DEV_VISION_PROBE" if "vision" in lower else "DEV_LLM_PROBE"
        return LlmUsageContext("DEV", purpose, conversation_id=value)
    if lower == "doctor":
        return LlmUsageContext("DEV", "DOCTOR_PROBE", conversation_id=value)
    return LlmUsageContext(conversation_id=value)


def provider_label(base_url: str) -> str:
    host = (urlparse(str(base_url or "")).hostname or "").strip().lower()
    return host or "openai-compatible"


class LlmUsageStore:
    """Small append-only LLM meter stored beside Character Memory facts."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA busy_timeout=10000")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS llm_calls(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    logical_call_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    input_chars INTEGER NOT NULL,
                    output_chars INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    vision_images INTEGER NOT NULL DEFAULT 0,
                    json_mode INTEGER NOT NULL DEFAULT 0,
                    usage_source TEXT NOT NULL DEFAULT 'UNAVAILABLE',
                    error_type TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_llm_calls_created
                    ON llm_calls(created_at_epoch DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_feature
                    ON llm_calls(feature,purpose,created_at_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_model
                    ON llm_calls(model,created_at_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_logical
                    ON llm_calls(logical_call_id,attempt);
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def add(self, item: dict[str, Any]) -> int:
        now = datetime.now(timezone.utc)
        created_at = str(item.get("created_at") or now.isoformat())
        created_at_epoch = int(item.get("created_at_epoch") or time.time() * 1_000_000)
        fields = (
            created_at,
            created_at_epoch,
            str(item.get("provider") or ""),
            str(item.get("model") or ""),
            str(item.get("feature") or "OTHER"),
            str(item.get("purpose") or "OTHER"),
            str(item.get("session_id") or ""),
            str(item.get("conversation_id") or ""),
            str(item.get("character_id") or ""),
            str(item.get("logical_call_id") or ""),
            max(1, int(item.get("attempt") or 1)),
            str(item.get("status") or "SUCCESS"),
            item.get("input_tokens"),
            item.get("output_tokens"),
            item.get("total_tokens"),
            max(0, int(item.get("input_chars") or 0)),
            max(0, int(item.get("output_chars") or 0)),
            max(0.0, float(item.get("duration_ms") or 0.0)),
            max(0, int(item.get("vision_images") or 0)),
            1 if item.get("json_mode") else 0,
            str(item.get("usage_source") or "UNAVAILABLE"),
            str(item.get("error_type") or ""),
            str(item.get("request_id") or ""),
        )
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO llm_calls(
                    created_at,created_at_epoch,provider,model,feature,purpose,
                    session_id,conversation_id,character_id,logical_call_id,
                    attempt,status,input_tokens,output_tokens,total_tokens,
                    input_chars,output_chars,duration_ms,vision_images,json_mode,
                    usage_source,error_type,request_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                fields,
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def _since_epoch(self, hours: int) -> int:
        hours = max(1, min(24 * 90, int(hours)))
        return int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1_000_000)

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def usage(self, *, hours: int = 24, limit: int = 80) -> dict[str, Any]:
        since = self._since_epoch(hours)
        limit = max(1, min(300, int(limit)))
        aggregate_sql = """
            SELECT
                COUNT(*) AS requests,
                COUNT(DISTINCT logical_call_id) AS logical_calls,
                COALESCE(SUM(input_tokens),0) AS input_tokens,
                COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS token_known_requests,
                COUNT(DISTINCT CASE WHEN attempt>1 THEN logical_call_id END) AS retried_logical_calls,
                SUM(CASE WHEN status!='SUCCESS' THEN 1 ELSE 0 END) AS errors,
                ROUND(AVG(duration_ms),1) AS avg_latency_ms,
                COALESCE(SUM(input_chars),0) AS input_chars,
                COALESCE(SUM(output_chars),0) AS output_chars
            FROM llm_calls WHERE created_at_epoch>=?
        """
        grouped_sql = """
            SELECT
                {group_cols},
                COUNT(*) AS requests,
                COUNT(DISTINCT logical_call_id) AS logical_calls,
                COALESCE(SUM(input_tokens),0) AS input_tokens,
                COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS token_known_requests,
                COUNT(DISTINCT CASE WHEN attempt>1 THEN logical_call_id END) AS retried_logical_calls,
                SUM(CASE WHEN status!='SUCCESS' THEN 1 ELSE 0 END) AS errors,
                ROUND(AVG(duration_ms),1) AS avg_latency_ms
            FROM llm_calls
            WHERE created_at_epoch>=?
            GROUP BY {group_cols}
            ORDER BY total_tokens DESC, requests DESC
        """
        with self._lock:
            summary_row = self.conn.execute(aggregate_sql, (since,)).fetchone()
            by_feature = self.conn.execute(
                grouped_sql.format(group_cols="feature,purpose"),
                (since,),
            ).fetchall()
            by_model = self.conn.execute(
                grouped_sql.format(group_cols="model"),
                (since,),
            ).fetchall()
            recent = self.conn.execute(
                """
                SELECT id,created_at,provider,model,feature,purpose,session_id,
                       conversation_id,character_id,logical_call_id,attempt,status,
                       input_tokens,output_tokens,total_tokens,input_chars,output_chars,
                       duration_ms,vision_images,json_mode,usage_source,error_type,request_id
                FROM llm_calls
                WHERE created_at_epoch>=?
                ORDER BY created_at_epoch DESC,id DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        summary = self._row_dict(summary_row)
        requests = int(summary.get("requests") or 0)
        known = int(summary.get("token_known_requests") or 0)
        summary["token_coverage"] = round(known / requests, 4) if requests else 1.0
        return {
            "window_hours": max(1, min(24 * 90, int(hours))),
            "summary": summary,
            "by_feature": [self._row_dict(row) for row in by_feature],
            "by_model": [self._row_dict(row) for row in by_model],
            "recent": [self._row_dict(row) for row in recent],
        }


class LlmUsageRecorder:
    def __init__(self, db_path: str | Path):
        self.store = LlmUsageStore(db_path)

    def close(self) -> None:
        self.store.close()

    def record(self, item: dict[str, Any]) -> int | None:
        try:
            return self.store.add(item)
        except Exception:
            # Usage telemetry must never break a real chat/model call.
            return None
