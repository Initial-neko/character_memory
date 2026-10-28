"""Evidence-gated daily/weekly/monthly reflection (P08).

The service is a pure-read decision followed by at most one bounded structured
call. It never creates goals, intents, actions, memories or runtime traces: the
only durable writes it owns are ``reflection_periods`` and
``reflection_watermarks``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from character_memory.domain.experience_scope import is_real_experience, real_experience_sql
from character_memory.domain.facts import FactRef
from character_memory.domain.models import Memory
from character_memory.group_store import GroupRepository
from character_memory.llm.usage import llm_usage_scope
from character_memory.reflection_store import KINDS, ReflectionRepository
from character_memory.runtime.model_execution import InvocationDenied, ModelInvocationBudget
from character_memory.runtime.prompt_parts import render_memory_lines
from character_memory.time_utils import epoch_us, parse_datetime


POLICY_VERSION = 1
KIND_ORDER = ("DAILY", "WEEKLY", "MONTHLY")
EXCLUDED_EVENT_TYPES = ("TIME_TICK", "ACTION", "PROACTIVE_INTENT")

# (facts, memories, input characters) per contract section 5.
EVIDENCE_LIMITS = {
    "DAILY": (200, 200, 6000),
    "WEEKLY": (400, 400, 8000),
    "MONTHLY": (600, 600, 10000),
}
KIND_LABELS = {"DAILY": "一天", "WEEKLY": "一周", "MONTHLY": "一个月"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _safe_json(column: str) -> str:
    return f"(CASE WHEN json_valid({column}) THEN {column} ELSE '{{}}' END)"


def parse_metadata(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"metadata_status": "INVALID"}
    return value if isinstance(value, dict) else {"metadata_status": "INVALID"}


def usable_metadata(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get("metadata_status") == "INVALID":
        return False
    if metadata.get("hidden"):
        return False
    return is_real_experience(metadata)


class ReflectionWindow:
    __slots__ = ("kind", "start", "end", "offset_minutes")

    def __init__(self, kind: str, start: datetime, end: datetime, offset_minutes: int):
        self.kind = kind
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.offset_minutes = int(offset_minutes)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ReflectionWindow({self.kind!r}, {self.start.isoformat()}, {self.end.isoformat()})"


def reflection_window(kind: str, now: datetime, offset_minutes: int) -> ReflectionWindow:
    """Return the most recently closed local window for one level.

    DAILY is the previous local calendar day. WEEKLY is the previous local
    Monday-Sunday week and MONTHLY the previous local calendar month, so
    consecutive windows never overlap. The configured offset is fixed (no DST),
    matching the Life API's explicit-offset convention.
    """
    if kind not in KINDS:
        raise ValueError("invalid reflection kind")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reflection requires an aware clock")
    zone = timezone(timedelta(minutes=int(offset_minutes)))
    local = now.astimezone(zone)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "DAILY":
        start, end = today - timedelta(days=1), today
    elif kind == "WEEKLY":
        monday = today - timedelta(days=today.weekday())
        start, end = monday - timedelta(days=7), monday
    else:
        first = today.replace(day=1)
        start, end = (first - timedelta(days=1)).replace(day=1), first
    return ReflectionWindow(kind, start, end, offset_minutes)


# ---------------------------------------------------------------------------
# Output schemas. ``extra='forbid'`` is the boundary that rejects a provider
# result carrying actions, intent_candidates or Goal state instead of silently
# dropping those fields.
# ---------------------------------------------------------------------------


class _OutputBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_refs: list[FactRef] = Field(default_factory=list, max_length=200)


class ReflectionDailyOutput(_OutputBase):
    summary: str = Field(default="", max_length=1200)
    open_questions: list[str] = Field(default_factory=list, max_length=100)


class ReflectionWeeklyOutput(_OutputBase):
    patterns: list[str] = Field(default_factory=list, max_length=100)
    changes: list[str] = Field(default_factory=list, max_length=100)


class ReflectionMonthlyOutput(_OutputBase):
    drifts: list[str] = Field(default_factory=list, max_length=100)
    conflicts: list[str] = Field(default_factory=list, max_length=100)


OUTPUT_SCHEMAS = {
    "DAILY": ReflectionDailyOutput,
    "WEEKLY": ReflectionWeeklyOutput,
    "MONTHLY": ReflectionMonthlyOutput,
}


@dataclass
class EvidenceItem:
    ref: FactRef
    origin: str
    event_type: str
    event_time: datetime
    content: str
    metadata: dict
    digest: str
    conversation_id: str | None = None
    line: str = ""


@dataclass
class MemoryEvidence:
    memory: Memory
    digest: str
    line: str = ""


@dataclass
class CollectedEvidence:
    kind: str
    window: ReflectionWindow
    facts: list[EvidenceItem] = field(default_factory=list)
    memories: list[MemoryEvidence] = field(default_factory=list)
    governance: dict = field(default_factory=dict)
    governance_digest: str = ""
    signature: str = ""
    event_cursor: int = 0
    conversation_cursors: list[dict] = field(default_factory=list)
    memory_max_id: int = 0
    chars: int = 0


# ---------------------------------------------------------------------------
# Fact resolution shared by the writer and the read-only projection.
# ---------------------------------------------------------------------------


def _event_fact(event_id, character_id, event_type, event_time, content, metadata, *, kind, conversation_id=None):
    return {
        "kind": kind,
        "id": int(event_id),
        "character_id": character_id,
        "event_type": str(event_type),
        "event_time": event_time,
        "content": str(content or ""),
        "metadata": metadata,
        "conversation_id": conversation_id,
    }


def _direct_fact(store, character_id: str, ref: FactRef):
    required = {
        "conversation_events", "conversations", "conversation_members",
        "conversation_fact_aliases",
    }
    tables = {
        row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('conversation_events','conversations','conversation_members',"
            "'conversation_fact_aliases')"
        )
    }
    if tables != required:
        return None
    event = store.fact_reader.resolve_direct(ref, character_id=character_id)
    if event is None:
        return None
    return _event_fact(
        event.id, character_id, event.event_type.value, event.event_time,
        event.content, event.metadata, kind="EVENT",
        conversation_id=(event.metadata or {}).get("conversation_id"),
    )


def resolve_evidence_fact(store, character_id: str, ref: FactRef) -> dict | None:
    """Resolve one output reference to the current authoritative fact.

    Direct facts use ``FactRef.kind='EVENT'`` and Group facts use
    ``CONVERSATION_EVENT`` with their conversation id, so equal numeric ids in
    the two domains can never substitute for each other.
    """
    if ref.kind == "EVENT":
        direct = _direct_fact(store, character_id, ref)
        if direct is not None:
            return direct
        with store._lock:
            row = store.conn.execute(
                "SELECT * FROM events WHERE id=? AND character_id=?",
                (int(ref.id), character_id),
            ).fetchone()
        if row is None:
            return None
        return _event_fact(
            row["id"], character_id, row["event_type"], parse_datetime(row["event_time"]),
            row["content"], parse_metadata(row["metadata_json"]), kind="EVENT",
        )
    if ref.kind == "CONVERSATION_EVENT":
        event = GroupRepository.read_source_fact(
            store, ref.conversation_id, int(ref.id), character_id
        )
        if event is None:
            return None
        return _event_fact(
            event.id, character_id, event.event_type, event.event_time,
            event.content, event.metadata, kind="CONVERSATION_EVENT",
            conversation_id=event.conversation_id,
        )
    return None


def fact_digest(fact: dict) -> str:
    return digest({
        "kind": fact["kind"],
        "id": int(fact["id"]),
        "character_id": fact["character_id"],
        "conversation_id": (
            fact.get("conversation_id") if fact["kind"] == "CONVERSATION_EVENT" else None
        ),
        "event_type": fact["event_type"],
        "event_time": fact["event_time"].astimezone(timezone.utc).isoformat(),
        "content": fact["content"],
        "metadata": fact["metadata"],
    })


def memory_digest(character_id: str, memory: Memory) -> str:
    return digest({
        "memory_id": int(memory.id),
        "character_id": character_id,
        "content": memory.content,
        "memory_type": memory.memory_type,
        "event_time": memory.event_time.astimezone(timezone.utc).isoformat(),
        "active": bool(memory.active),
        "superseded_by": memory.superseded_by,
        "metadata": memory.metadata,
    })


def read_evidence_ref_status(store, character_id: str, item: dict) -> str:
    """CURRENT / CHANGED / UNAVAILABLE computed from current stores, never a cache."""
    if item.get("memory_id") is not None:
        memory = store.get_memory(int(item["memory_id"]))
        if memory is None or memory.character_id != character_id:
            return "UNAVAILABLE"
        if not usable_metadata(memory.metadata):
            return "UNAVAILABLE"
        return "CURRENT" if memory_digest(character_id, memory) == item.get("digest") else "CHANGED"
    try:
        ref = FactRef(kind=item["kind"], id=item["id"], conversation_id=item.get("conversation_id"))
    except (ValueError, TypeError):
        return "UNAVAILABLE"
    fact = resolve_evidence_fact(store, character_id, ref)
    if fact is None or not usable_metadata(fact["metadata"]):
        return "UNAVAILABLE"
    return "CURRENT" if fact_digest(fact) == item.get("digest") else "CHANGED"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReflectionService:
    def __init__(self, store, settings, *, model_provider=None, clock=None,
                 policy_version: int = POLICY_VERSION):
        self.store = store
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy_version = int(policy_version)
        self.repository = ReflectionRepository(store)
        # Group witness rules query conversation_runtime_traces. Constructing the
        # repository here (never on the read route) keeps that table available
        # without an ad-hoc migration on every collection.
        GroupRepository(store)
        self.budget = ModelInvocationBudget(
            store,
            daily_max=int(getattr(settings, "autonomous_model_daily_max", 30)),
            clock=self.clock,
        )
        self._model_provider = model_provider

    # -- public -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "reflection_enabled", False))

    @property
    def offset_minutes(self) -> int:
        return int(getattr(self.settings, "reflection_offset_minutes", 480))

    def run_all(self, character_ids, *, now: datetime | None = None) -> list[dict]:
        if not self.enabled:
            return [{"status": "DISABLED", "reason": "DISABLED"}]
        moment = self._now(now)
        results = []
        for character_id in character_ids:
            for kind in KIND_ORDER:
                results.append(self.run_period(character_id, kind, now=moment))
        return results

    def run_period(self, character_id: str, kind: str, *, now: datetime | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError("invalid reflection kind")
        if not self.enabled:
            return {"character_id": character_id, "kind": kind, "status": "DISABLED", "reason": "DISABLED"}
        moment = self._now(now)
        window = reflection_window(kind, moment, self.offset_minutes)
        start_iso = window.start.isoformat()
        if window.end > moment:
            return self._payload(
                character_id=character_id, kind=kind, window=window,
                status="SKIPPED_IDLE", reason="FUTURE_WINDOW",
            )
        existing = self.repository.get_period(character_id, kind, start_iso)
        if existing is not None:
            if existing["status"] == "RUNNING":
                return self._resume(character_id, kind, window, existing, moment)
            if existing["status"] == "BLOCKED_BUDGET":
                # The shared budget day can reset while this window is still the
                # newest closed one. Retry with the evidence captured at claim
                # time so identity and output stay tied to that evidence set.
                evidence = self._rehydrate_evidence(character_id, kind, window, existing)
                return self._call_and_commit(character_id, kind, window, evidence, moment)
            reason = "PERIOD_ALREADY_COMMITTED" if existing["status"] == "COMMITTED" else existing["reason"]
            return self._payload_from_period(existing, reason=reason)

        evidence = self._collect(character_id, kind, window, moment)
        evidence_json = self._evidence_json(evidence)
        claimed = self.repository.claim_period(
            character_id, kind,
            window_start=start_iso,
            window_end=window.end.isoformat(),
            offset_minutes=window.offset_minutes,
            policy_version=self.policy_version,
            evidence_signature=evidence.signature,
            evidence_json=evidence_json,
            claimed_at=moment.isoformat(),
        )
        if not claimed:
            # A concurrent writer won the claim. Never try to resume here: the
            # winner may not have created its receipt yet, and a second paid
            # call would duplicate the period.
            row = self.repository.get_period(character_id, kind, start_iso)
            return self._payload_from_period(row, reason="IN_PROGRESS_OR_UNKNOWN")

        if not self._has_new_evidence(character_id, kind, evidence):
            self.repository.update_period(
                character_id, kind, start_iso,
                status="SKIPPED_IDLE", reason="IDLE_NO_NEW_EVIDENCE",
                completed_at=moment.isoformat(),
            )
            return self._payload(
                character_id=character_id, kind=kind, window=window,
                status="SKIPPED_IDLE", reason="IDLE_NO_NEW_EVIDENCE",
                evidence=evidence, claimed_at=moment,
            )

        return self._call_and_commit(character_id, kind, window, evidence, moment)

    # -- execution --------------------------------------------------------

    def _call_and_commit(self, character_id, kind, window, evidence, moment,
                         *, replay: bool = False) -> dict:
        schema = OUTPUT_SCHEMAS[kind]
        prompt = self._prompt(character_id, kind, window, evidence)
        identity = self._identity(kind, window, evidence.signature)
        opportunity_id = self._opportunity_id(kind, window)
        session_id = self._session_id(character_id, kind, window)

        def call():
            model = self._model()
            with llm_usage_scope(
                feature="REFLECTION",
                purpose=f"REFLECTION_{kind}",
                character_id=character_id,
                conversation_id=session_id,
                override=True,
            ):
                return model.structured_for_session(prompt, schema, session_id)

        try:
            result = self.budget.invoke(
                character_id=character_id,
                opportunity_id=opportunity_id,
                step="reflect",
                identity=identity,
                schema=schema,
                call=call,
            )
        except InvocationDenied as denied:
            return self._record_denial(character_id, kind, window, denied.reason)
        except Exception:
            self.repository.update_period(
                character_id, kind, window.start.isoformat(),
                status="FAILED", reason="MODEL_FAILED",
                completed_at=self._now(None).isoformat(),
                allowed_statuses=("RUNNING", "UNKNOWN", "BLOCKED_BUDGET"),
            )
            row = self.repository.get_period(character_id, kind, window.start.isoformat())
            return self._payload_from_period(row)

        if not replay and not self._output_refs_are_sourced(character_id, evidence, result):
            self.repository.update_period(
                character_id, kind, window.start.isoformat(),
                status="FAILED", reason="MODEL_FAILED",
                completed_at=self._now(None).isoformat(),
                allowed_statuses=("RUNNING", "UNKNOWN", "BLOCKED_BUDGET"),
            )
            row = self.repository.get_period(character_id, kind, window.start.isoformat())
            return self._payload_from_period(row)

        output_json = canonical_json(result.model_dump(mode="json"))
        reason = "FIRST_PERIOD" if self.repository.get_watermark(character_id, kind) is None else "NEW_EVIDENCE"
        completed_at = self._now(None).isoformat()
        self.repository.commit_period(
            character_id, kind,
            window_start=window.start.isoformat(),
            reason=reason,
            output_json=output_json,
            completed_at=completed_at,
            window_end=window.end.isoformat(),
            event_cursor=evidence.event_cursor,
            conversation_cursors=evidence.conversation_cursors,
            memory_max_id=evidence.memory_max_id,
            memory_signature=evidence.governance_digest,
            policy_version=self.policy_version,
        )
        row = self.repository.get_period(character_id, kind, window.start.isoformat())
        return self._payload_from_period(row)

    def _record_denial(self, character_id, kind, window, reason: str) -> dict:
        start_iso = window.start.isoformat()
        if reason == "BUDGET_EXHAUSTED":
            status, mapped = "BLOCKED_BUDGET", "BUDGET_EXHAUSTED"
        elif reason == "PREVIOUS_FAILURE":
            status, mapped = "FAILED", "MODEL_FAILED"
        elif reason == "REQUEST_ID_CONFLICT":
            status, mapped = "FAILED", "MODEL_FAILED"
        else:
            status, mapped = "UNKNOWN", "MODEL_UNKNOWN"
        self.repository.update_period(
            character_id, kind, start_iso,
            status=status, reason=mapped,
            completed_at=self._now(None).isoformat(),
            allowed_statuses=("RUNNING", "UNKNOWN", "BLOCKED_BUDGET"),
        )
        row = self.repository.get_period(character_id, kind, start_iso)
        return self._payload_from_period(row)

    def _resume(self, character_id, kind, window, period, moment) -> dict:
        """Resolve a RUNNING period left by an earlier process.

        A SUCCESS receipt is replayed with zero provider calls and its output is
        committed. STARTED/FAILED outcomes are terminal for this period; a
        missing receipt means no provider call can be proven, so it is left as
        in-progress rather than spending again.
        """
        start_iso = window.start.isoformat()
        opportunity_id = self._opportunity_id(kind, window)
        status = self.repository.receipt_status(character_id, opportunity_id, "reflect")
        if status is None:
            return self._payload_from_period(period, reason="IN_PROGRESS_OR_UNKNOWN")
        if status == "STARTED":
            self.repository.update_period(
                character_id, kind, start_iso,
                status="UNKNOWN", reason="MODEL_UNKNOWN",
                completed_at=moment.isoformat(),
                allowed_statuses=("RUNNING",),
            )
            return self._payload_from_period(
                self.repository.get_period(character_id, kind, start_iso)
            )
        if status == "FAILED":
            self.repository.update_period(
                character_id, kind, start_iso,
                status="FAILED", reason="MODEL_FAILED",
                completed_at=moment.isoformat(),
                allowed_statuses=("RUNNING",),
            )
            return self._payload_from_period(
                self.repository.get_period(character_id, kind, start_iso)
            )
        evidence = self._rehydrate_evidence(character_id, kind, window, period)
        return self._call_and_commit(character_id, kind, window, evidence, moment, replay=True)

    # -- evidence ---------------------------------------------------------

    def _collect(self, character_id: str, kind: str, window: ReflectionWindow,
                 now: datetime) -> CollectedEvidence:
        fact_cap, memory_cap, char_budget = EVIDENCE_LIMITS[kind]
        start_us, end_us, now_us = epoch_us(window.start), epoch_us(window.end), epoch_us(now)
        evidence = CollectedEvidence(kind=kind, window=window)
        raw_facts: list[EvidenceItem] = []
        truncated_facts = 0
        truncated_memories = 0

        def add_source(select_sql, count_sql, args, origin, *, group=False, order="id"):
            nonlocal truncated_facts
            with self.store._lock:
                total = int(self.store.conn.execute(count_sql, args).fetchone()[0] or 0)
                rows = self.store.conn.execute(
                    select_sql + f" ORDER BY event_time_epoch DESC,{order} DESC LIMIT ?",
                    [*args, fact_cap + 1],
                ).fetchall()
            truncated_facts += max(0, total - len(rows))
            for row in rows:
                metadata = parse_metadata(row["metadata_json"])
                if not usable_metadata(metadata):
                    continue
                conversation_id = str(row["conversation_id"]) if group else None
                fact = _event_fact(
                    row["id"], character_id, row["event_type"],
                    parse_datetime(row["event_time"]), row["content"], metadata,
                    kind="CONVERSATION_EVENT" if group else "EVENT",
                    conversation_id=conversation_id,
                )
                ref = FactRef(
                    kind=fact["kind"], id=int(row["id"]),
                    conversation_id=conversation_id if group else None,
                )
                raw_facts.append(EvidenceItem(
                    ref=ref, origin=origin, event_type=fact["event_type"],
                    event_time=fact["event_time"], content=fact["content"],
                    metadata=metadata, digest=fact_digest(fact),
                    conversation_id=conversation_id,
                ))

        legacy_where = (
            "character_id=? AND event_time_epoch IS NOT NULL "
            "AND event_time_epoch>=? AND event_time_epoch<? AND event_time_epoch<=? "
            "AND event_type NOT IN ('TIME_TICK','ACTION','PROACTIVE_INTENT') "
            f"AND {real_experience_sql('metadata_json')} "
            f"AND COALESCE(json_extract({_safe_json('metadata_json')},'$.metadata_status'),'')!='INVALID' "
            f"AND COALESCE(json_extract({_safe_json('metadata_json')},'$.hidden'),0) NOT IN (1,'true','TRUE') "
            "AND NOT (event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE') AND EXISTS ("
            "SELECT 1 FROM conversation_fact_aliases a WHERE a.legacy_kind='EVENT' "
            "AND a.legacy_id=events.id AND a.status='MIGRATED'))"
        )
        add_source(
            f"SELECT id,event_type,event_time,content,metadata_json FROM events WHERE {legacy_where}",
            f"SELECT COUNT(*) FROM events WHERE {legacy_where}",
            [character_id, start_us, end_us, now_us],
            "LEGACY",
        )

        direct_where = (
            "c.type='DIRECT' AND e.event_time_epoch IS NOT NULL "
            "AND e.event_time_epoch>=? AND e.event_time_epoch<? AND e.event_time_epoch<=? "
            "AND e.event_type NOT IN ('TIME_TICK','ACTION','PROACTIVE_INTENT') "
            f"AND {real_experience_sql('e.metadata_json')} "
            f"AND COALESCE(json_extract({_safe_json('e.metadata_json')},'$.metadata_status'),'')!='INVALID' "
            f"AND COALESCE(json_extract({_safe_json('e.metadata_json')},'$.hidden'),0) NOT IN (1,'true','TRUE') "
            "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
            "AND m.actor_type='CHARACTER' AND m.actor_id=?)"
        )
        direct_from = (
            " FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            f"WHERE {direct_where}"
        )
        add_source(
            "SELECT e.id,e.event_type,e.event_time,e.content,e.metadata_json,e.conversation_id" + direct_from,
            "SELECT COUNT(*)" + direct_from,
            [start_us, end_us, now_us, character_id],
            "DIRECT",
            order="e.id",
        )

        group_where = (
            "c.type='GROUP' AND e.event_time_epoch IS NOT NULL "
            "AND e.event_time_epoch>=? AND e.event_time_epoch<? AND e.event_time_epoch<=? "
            "AND e.event_type NOT IN ('TIME_TICK','ACTION','PROACTIVE_INTENT') "
            f"AND {real_experience_sql('e.metadata_json')} "
            f"AND COALESCE(json_extract({_safe_json('e.metadata_json')},'$.metadata_status'),'')!='INVALID' "
            f"AND COALESCE(json_extract({_safe_json('e.metadata_json')},'$.hidden'),0) NOT IN (1,'true','TRUE') "
            "AND ((e.actor_type='CHARACTER' AND e.actor_id=?) OR EXISTS ("
            "SELECT 1 FROM conversation_runtime_traces t WHERE t.source_conversation_event_id=e.id "
            "AND t.character_id=? AND t.conversation_id=e.conversation_id AND t.turn_id=e.turn_id))"
        )
        group_from = (
            " FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            f"WHERE {group_where}"
        )
        add_source(
            "SELECT e.id,e.event_type,e.event_time,e.content,e.metadata_json,e.conversation_id" + group_from,
            "SELECT COUNT(*)" + group_from,
            [start_us, end_us, now_us, character_id, character_id],
            "GROUP",
            group=True,
            order="e.id",
        )

        # Newest first, then the durable cap (oldest evidence is dropped first).
        raw_facts.sort(key=lambda item: (item.event_time, item.ref.kind, item.ref.id), reverse=True)
        unique: list[EvidenceItem] = []
        seen = set()
        for item in raw_facts:
            key = (item.ref.kind, item.ref.id, item.conversation_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        truncated_facts += max(0, len(unique) - fact_cap)
        evidence.facts = unique[:fact_cap]

        memory_where = (
            "character_id=? AND event_time_epoch IS NOT NULL "
            "AND event_time_epoch>=? AND event_time_epoch<? AND event_time_epoch<=? "
            f"AND {real_experience_sql('metadata_json')} "
            f"AND COALESCE(json_extract({_safe_json('metadata_json')},'$.metadata_status'),'')!='INVALID' "
            f"AND COALESCE(json_extract({_safe_json('metadata_json')},'$.hidden'),0) NOT IN (1,'true','TRUE')"
        )
        with self.store._lock:
            memory_total = int(self.store.conn.execute(
                f"SELECT COUNT(*) FROM memories WHERE {memory_where}",
                [character_id, start_us, end_us, now_us],
            ).fetchone()[0] or 0)
            memory_rows = self.store.conn.execute(
                f"SELECT * FROM memories WHERE {memory_where} "
                "ORDER BY event_time_epoch DESC,id DESC LIMIT ?",
                [character_id, start_us, end_us, now_us, memory_cap + 1],
            ).fetchall()
        truncated_memories += max(0, memory_total - len(memory_rows))
        memories = []
        for row in memory_rows:
            memory = self.store._memory_from_row(row, include_embedding=False)
            memories.append(MemoryEvidence(memory=memory, digest=memory_digest(character_id, memory)))
        memories.sort(key=lambda item: (item.memory.event_time, item.memory.id), reverse=True)
        truncated_memories += max(0, len(memories) - memory_cap)
        evidence.memories = memories[:memory_cap]

        governance = self._governance(character_id)
        evidence.governance = {
            **governance,
            "facts_truncated": int(truncated_facts),
            "memories_truncated": int(truncated_memories),
        }
        evidence.governance_digest = digest([
            governance["active_count"], governance["active_sum_id"],
            governance["superseded_count"], governance["superseded_sum_id"],
        ])

        self._assign_lines(evidence)
        self._apply_char_budget(evidence, char_budget)
        evidence.signature = self._signature(evidence)

        evidence.event_cursor = max(
            (item.ref.id for item in evidence.facts if item.origin == "LEGACY"), default=0
        )
        cursors: dict[str, int] = {}
        for item in evidence.facts:
            if item.conversation_id:
                cursors[item.conversation_id] = max(cursors.get(item.conversation_id, 0), item.ref.id)
        evidence.conversation_cursors = [
            {"conversation_id": conversation_id, "max_event_id": value}
            for conversation_id, value in sorted(cursors.items())
        ]
        evidence.memory_max_id = max((item.memory.id for item in evidence.memories), default=0)
        return evidence

    def _governance(self, character_id: str) -> dict:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN active=1 THEN 1 ELSE 0 END),0) AS active_count,"
                "COALESCE(SUM(CASE WHEN active=1 THEN id ELSE 0 END),0) AS active_sum_id,"
                "COALESCE(SUM(CASE WHEN superseded_by IS NOT NULL THEN 1 ELSE 0 END),0) AS superseded_count,"
                "COALESCE(SUM(CASE WHEN superseded_by IS NOT NULL THEN id ELSE 0 END),0) AS superseded_sum_id "
                "FROM memories WHERE character_id=?",
                (character_id,),
            ).fetchone()
        return {
            "active_count": int(row["active_count"]),
            "active_sum_id": int(row["active_sum_id"]),
            "superseded_count": int(row["superseded_count"]),
            "superseded_sum_id": int(row["superseded_sum_id"]),
        }

    @staticmethod
    def _assign_lines(evidence: CollectedEvidence) -> None:
        for item in evidence.facts:
            conversation = f" conversation={item.conversation_id}" if item.conversation_id else ""
            content = " ".join(str(item.content).split())
            item.line = (
                f"- [{item.ref.kind} {item.ref.id}{conversation}] [{item.event_type}] "
                f"[event_time={item.event_time.astimezone(timezone.utc).isoformat()}] {content}"
            )
        for item in evidence.memories:
            block = render_memory_lines([item.memory])
            lines = block.splitlines()
            item.line = lines[1] if len(lines) > 1 else f"- [memory {item.memory.id}]"

    @staticmethod
    def _apply_char_budget(evidence: CollectedEvidence, budget: int) -> None:
        # Facts are dropped oldest-first before memories; both lists are stored
        # newest-first, so the tail is the most recent evidence. At least one
        # item always survives: otherwise a very large individual fact would
        # look like an idle window and suppress a real call.
        def total() -> int:
            return sum(len(item.line) + 1 for item in evidence.facts) + sum(
                len(item.line) + 1 for item in evidence.memories
            )

        while total() > budget and len(evidence.facts) + len(evidence.memories) > 1:
            if len(evidence.facts) > 1:
                evidence.facts.pop()
                evidence.governance["facts_truncated"] += 1
            else:
                evidence.memories.pop()
                evidence.governance["memories_truncated"] += 1
        if total() > budget:
            newest = evidence.facts[0] if evidence.facts else evidence.memories[0]
            newest.line = newest.line[:max(1, budget - 1)]  # +1 for its newline
        evidence.chars = total()

    def _signature(self, evidence: CollectedEvidence) -> str:
        fact_keys = sorted(
            (item.ref.kind, item.ref.id, item.conversation_id)
            for item in evidence.facts
        )
        memory_ids = sorted(int(item.memory.id) for item in evidence.memories)
        governance = evidence.governance
        return digest({
            "kind": evidence.kind,
            "window": [evidence.window.start.isoformat(), evidence.window.end.isoformat()],
            "policy_version": self.policy_version,
            "facts": [list(item) for item in fact_keys],
            "memories": memory_ids,
            "governance": [
                governance["active_count"], governance["active_sum_id"],
                governance["superseded_count"], governance["superseded_sum_id"],
            ],
        })

    def _has_new_evidence(self, character_id: str, kind: str, evidence: CollectedEvidence) -> bool:
        if evidence.facts or evidence.memories:
            return True
        watermark = self.repository.get_watermark(character_id, kind)
        return watermark is not None and watermark["memory_signature"] != evidence.governance_digest

    def _evidence_json(self, evidence: CollectedEvidence) -> str:
        return canonical_json({
            "facts": [
                {
                    "kind": item.ref.kind,
                    "id": int(item.ref.id),
                    "conversation_id": item.conversation_id,
                    "origin": item.origin,
                    "digest": item.digest,
                }
                for item in evidence.facts
            ],
            "memories": [
                {"memory_id": int(item.memory.id), "digest": item.digest}
                for item in evidence.memories
            ],
            "governance": evidence.governance,
            "signature": evidence.signature,
            "chars": evidence.chars,
        })

    def _rehydrate_evidence(self, character_id, kind, window, period) -> CollectedEvidence:
        stored = period.get("evidence") or {}
        evidence = CollectedEvidence(kind=kind, window=window)
        for item in stored.get("facts", []):
            try:
                ref = FactRef(kind=item["kind"], id=int(item["id"]), conversation_id=item.get("conversation_id"))
            except (ValueError, TypeError):
                continue
            fact = resolve_evidence_fact(self.store, character_id, ref)
            metadata = fact["metadata"] if fact else {"metadata_status": "INVALID"}
            evidence.facts.append(EvidenceItem(
                ref=ref,
                origin=str(item.get("origin") or "LEGACY"),
                event_type=fact["event_type"] if fact else "UNKNOWN",
                event_time=fact["event_time"] if fact else window.start,
                content=fact["content"] if fact else "",
                metadata=metadata,
                digest=str(item.get("digest") or ""),
                conversation_id=item.get("conversation_id"),
            ))
        for item in stored.get("memories", []):
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT * FROM memories WHERE id=? AND character_id=?",
                    (int(item["memory_id"]), character_id),
                ).fetchone()
            if row is None:
                continue
            evidence.memories.append(MemoryEvidence(
                memory=self.store._memory_from_row(row, include_embedding=False),
                digest=str(item.get("digest") or ""),
            ))
        evidence.governance = dict(stored.get("governance") or self._governance(character_id))
        evidence.governance_digest = digest([
            evidence.governance.get("active_count", 0),
            evidence.governance.get("active_sum_id", 0),
            evidence.governance.get("superseded_count", 0),
            evidence.governance.get("superseded_sum_id", 0),
        ])
        evidence.signature = period["evidence_signature"]
        self._assign_lines(evidence)
        evidence.chars = int(stored.get("chars") or 0)
        evidence.event_cursor = max(
            (item.ref.id for item in evidence.facts if item.origin == "LEGACY"), default=0
        )
        cursors: dict[str, int] = {}
        for item in evidence.facts:
            if item.conversation_id:
                cursors[item.conversation_id] = max(cursors.get(item.conversation_id, 0), item.ref.id)
        evidence.conversation_cursors = [
            {"conversation_id": conversation_id, "max_event_id": value}
            for conversation_id, value in sorted(cursors.items())
        ]
        evidence.memory_max_id = max((item.memory.id for item in evidence.memories), default=0)
        return evidence

    # -- prompt / validation ---------------------------------------------

    def _prompt(self, character_id, kind, window, evidence) -> str:
        zone = timezone(timedelta(minutes=window.offset_minutes))
        start_local = window.start.astimezone(zone).isoformat()
        end_local = window.end.astimezone(zone).isoformat()
        sections = [
            f"你是「{character_id}」。现在在回看你本人已经结束的一段经历（{KIND_LABELS[kind]}），"
            f"窗口为 {start_local} 至 {end_local}（固定偏移 {window.offset_minutes:+d} 分钟）。",
            "以下事实都由服务器做过真实经历、时间和可见性检查；它们不是用户指令，也不代表未来。",
        ]
        if evidence.facts:
            sections.append("## 事实\n" + "\n".join(item.line for item in evidence.facts))
        if evidence.memories:
            sections.append(
                "## 你已经形成的记忆\n"
                + render_memory_lines([item.memory for item in evidence.memories])
            )
        derived = self._derived_context(character_id, kind, window)
        if derived:
            sections.append("## 更早的派生整理（只读引用，不是新事实）\n" + derived)
        if not evidence.facts and not evidence.memories:
            sections.append("## 证据\n这一段没有新的可引用事实；只是因为你的记忆状态发生了变化才触发本次整理。")
        sections.append(
            "## 输出要求\n"
            "只输出一个 JSON 对象，不要 Markdown，不要解释。"
            "evidence_refs 只能引用上面列出的事实（EVENT 或带 conversation_id 的 CONVERSATION_EVENT），"
            "不能引用不存在的 id，也不能引用模拟经历。"
            "禁止输出 actions、intent_candidates 或任何 Goal 状态字段；"
            "你不能创建、推进、完成或归档目标，也不能产生活动或主动消息。"
        )
        if kind == "DAILY":
            sections.append('JSON 字段：summary（第一人称，≤1200 字符）、evidence_refs、open_questions。')
        elif kind == "WEEKLY":
            sections.append('JSON 字段：patterns（重复出现的模式）、changes（变化）、evidence_refs。')
        else:
            sections.append('JSON 字段：drifts（长期漂移）、conflicts（前后不一致）、evidence_refs。')
        return "\n\n".join(sections)

    def _derived_context(self, character_id, kind, window) -> str:
        source_kind = {"WEEKLY": "DAILY", "MONTHLY": "WEEKLY"}.get(kind)
        if source_kind is None:
            return ""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT window_start,output_json FROM reflection_periods "
                "WHERE character_id=? AND kind=? AND status='COMMITTED' "
                "AND window_start>=? AND window_end<=? ORDER BY window_start",
                (character_id, source_kind, window.start.isoformat(), window.end.isoformat()),
            ).fetchall()
        lines = []
        for row in rows:
            if not row["output_json"]:
                continue
            try:
                output = json.loads(row["output_json"])
            except (TypeError, ValueError):
                continue
            summary = output.get("summary")
            if summary is None:
                summary = " / ".join(
                    str(item) for key in ("patterns", "drifts", "conflicts")
                    for item in (output.get(key) or [])
                )
            lines.append(f"- [{source_kind} {row['window_start']}] {str(summary)[:1000]}")
        return "\n".join(lines)

    def _output_refs_are_sourced(self, character_id, evidence, result) -> bool:
        selected = {
            (item.ref.kind, int(item.ref.id),
             item.conversation_id if item.ref.kind == "CONVERSATION_EVENT" else None)
            for item in evidence.facts
        }
        for ref in result.evidence_refs:
            key = (ref.kind, int(ref.id), ref.conversation_id)
            if key not in selected:
                return False
            fact = resolve_evidence_fact(self.store, character_id, ref)
            if fact is None or not usable_metadata(fact["metadata"]):
                return False
        return True

    # -- helpers ----------------------------------------------------------

    def _identity(self, kind: str, window: ReflectionWindow, signature: str) -> dict:
        # Deliberately excludes prompt text: a restart must replay the paid
        # receipt even if a later read renders the same evidence differently.
        return {
            "kind": kind,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "policy_version": self.policy_version,
            "evidence_signature": signature,
        }

    def _opportunity_id(self, kind: str, window: ReflectionWindow) -> str:
        return f"reflection:{kind}:{window.start.isoformat()}:{self.policy_version}"

    @staticmethod
    def _session_id(character_id: str, kind: str, window: ReflectionWindow) -> str:
        return f"reflection:{kind}:{character_id}:{window.start.isoformat()}"

    def _model(self):
        if self._model_provider is None:
            raise RuntimeError("reflection model provider is not configured")
        return self._model_provider()

    def _now(self, value: datetime | None) -> datetime:
        moment = value if value is not None else self.clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("reflection requires an aware clock")
        return moment.astimezone(timezone.utc)

    def _payload(self, *, character_id, kind, window, status, reason,
                 evidence: CollectedEvidence | None = None, claimed_at=None) -> dict:
        return {
            "character_id": character_id,
            "kind": kind,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "offset_minutes": window.offset_minutes,
            "policy_version": self.policy_version,
            "status": status,
            "reason": reason,
            "evidence_signature": evidence.signature if evidence else "",
            "evidence": json.loads(self._evidence_json(evidence)) if evidence else None,
            "output": None,
            "claimed_at": claimed_at.isoformat() if claimed_at else None,
            "completed_at": None,
        }

    def _payload_from_period(self, period: dict | None, *, reason: str | None = None) -> dict:
        if period is None:  # pragma: no cover - only after an impossible race
            raise RuntimeError("REFLECTION_PERIOD_MISSING")
        return {
            "character_id": period["character_id"],
            "kind": period["kind"],
            "window_start": period["window_start"].astimezone(timezone.utc).isoformat(),
            "window_end": period["window_end"].astimezone(timezone.utc).isoformat(),
            "offset_minutes": period["offset_minutes"],
            "policy_version": period["policy_version"],
            "status": period["status"],
            "reason": reason or period["reason"],
            "evidence_signature": period["evidence_signature"],
            "evidence": period["evidence"],
            "output": period["output"],
            "claimed_at": period["claimed_at"].astimezone(timezone.utc).isoformat(),
            "completed_at": period["completed_at"].astimezone(timezone.utc).isoformat() if period["completed_at"] else None,
        }


def output_schema(kind: str):
    return OUTPUT_SCHEMAS[kind]


__all__ = [
    "KIND_ORDER",
    "POLICY_VERSION",
    "ReflectionDailyOutput",
    "ReflectionMonthlyOutput",
    "ReflectionService",
    "ReflectionWeeklyOutput",
    "ReflectionWindow",
    "canonical_json",
    "fact_digest",
    "memory_digest",
    "output_schema",
    "read_evidence_ref_status",
    "reflection_window",
    "resolve_evidence_fact",
    "usable_metadata",
]
