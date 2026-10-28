"""Durable acceptance receipts, referencing existing authoritative message facts.

This records acceptance and last confirmed queue handoff, not model completion.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
import time

from character_memory.domain.facts import FactRef


class ReceiptConflict(ValueError):
    pass


@dataclass
class AcceptedMessage:
    response: dict
    created: bool
    dispatch: str


class MessageReceiptRepository:
    def __init__(self, store):
        self.store = store
        def migrate():
            store.conn.execute("""CREATE TABLE message_acceptance_receipts(
                scope TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, source_ref_json TEXT NOT NULL,
                response_json TEXT NOT NULL, dispatch TEXT NOT NULL,
                PRIMARY KEY(scope,request_id))""")
        store.apply_schema_migration("conversation/001-message-acceptance", migrate, immediate=True)

    def accept(self, scope: list[str], request_id: str, payload: dict, create, *, source_kind: str, conversation_id=None):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("invalid request_id")
        namespace = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute("SELECT * FROM message_acceptance_receipts WHERE scope=? AND request_id=?",
                (namespace, request_id)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ReceiptConflict("request_id already belongs to a different payload")
                ref = FactRef.model_validate_json(row["source_ref_json"])
                response = json.loads(row["response_json"])
                if not isinstance(response, dict) or response.get("event_id") != ref.id:
                    raise ReceiptConflict("stored acceptance receipt is invalid")
                if ref.kind == "EVENT":
                    # A Direct source can be a preserved legacy row or a unified
                    # conversation fact; the reader is the one place that knows.
                    fact = self.store.fact_reader.resolve_direct(ref, character_id=scope[1])
                    valid = fact is not None
                else:
                    # Direct facts share `conversation_events`, so id+conversation
                    # alone could let a mislabelled Direct ref pass as Group.
                    valid = self.store.conn.execute(
                        "SELECT 1 FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                        "WHERE e.id=? AND e.conversation_id=? AND c.type='GROUP'",
                        (ref.id, ref.conversation_id),
                    ).fetchone() is not None
                if not valid:
                    raise ReceiptConflict("accepted source fact is unavailable; do not repeat the request")
                return AcceptedMessage(response, False, row["dispatch"])
            response = create()
            ref = FactRef(kind=source_kind, id=response["event_id"], conversation_id=conversation_id)
            self.store.conn.execute("INSERT INTO message_acceptance_receipts VALUES(?,?,?,?,?,?)",
                (namespace, request_id, fingerprint, ref.model_dump_json(), json.dumps(response, ensure_ascii=False), "RECORDED"))
            return AcceptedMessage(response, True, "RECORDED")

    def mark_dispatch(self, scope: list[str], request_id: str, state: str):
        if state not in {"ENQUEUED", "UNKNOWN"}:
            raise ValueError("invalid dispatch state")
        namespace = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
        with self.store.transaction(immediate=True):
            self.store.conn.execute("UPDATE message_acceptance_receipts SET dispatch=? WHERE scope=? AND request_id=? AND dispatch='RECORDED'",
                (state, namespace, request_id))


class ExecutionUnavailable(RuntimeError):
    """A source has no safely repeatable execution at the requested version."""
    def __init__(self, state: str):
        self.state = state
        super().__init__(f"reaction execution is {state}; automatic replay is not allowed")


@dataclass
class ReactionClaim:
    source: FactRef
    character_id: str
    version: str
    token: str | None
    result: dict | None = None


class ReactionExecutionRepository:
    """Per-Person cognition receipts owned beside message acceptance receipts.

    Only explicitly expired PREPARING owners may be replaced. CALLING and
    unknown outcomes are never reclaimed by time; queue recovery is separate.
    """
    def __init__(self, store, *, preparation_lease_seconds=120.0, clock=None):
        lease = float(preparation_lease_seconds)
        if not math.isfinite(lease) or not 1 <= lease <= 3600:
            raise ValueError("preparation lease must be between 1 and 3600 seconds")
        self.store = store
        self.preparation_lease_seconds = lease
        self._clock = clock or time.time
        def migrate():
            store.conn.execute("""CREATE TABLE conversation_reaction_receipts(
                source_ref_json TEXT NOT NULL, character_id TEXT NOT NULL,
                reaction_version TEXT NOT NULL, state TEXT NOT NULL,
                owner_token TEXT NOT NULL, result_json TEXT, error_kind TEXT,
                PRIMARY KEY(source_ref_json,character_id,reaction_version))""")
        store.apply_schema_migration("conversation/002-reaction-execution", migrate, immediate=True)
        def add_preparation_deadline():
            store.conn.execute("ALTER TABLE conversation_reaction_receipts ADD COLUMN preparation_deadline REAL")
        store.apply_schema_migration("conversation/003-preparation-lease", add_preparation_deadline, immediate=True)

    def _now(self):
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("preparation clock must return finite server epoch seconds")
        return now

    @staticmethod
    def require_independent_boundary(store):
        # _tx_depth is connection-wide. Acquire the RLock before interpreting
        # it as this caller's transaction rather than another thread's work.
        with store._lock:
            if store._tx_depth:
                raise RuntimeError("reaction execution cannot run inside a caller transaction")

    @staticmethod
    def _key(claim):
        return (claim.source.model_dump_json(), claim.character_id, claim.version)

    def claim(self, source: FactRef, character_id: str, *, version="1", legacy_check=None):
        self.require_independent_boundary(self.store)
        from uuid import uuid4
        work = ReactionClaim(source, character_id, version, uuid4().hex)
        with self.store.transaction(immediate=True):
            now = self._now()
            row = self.store.conn.execute("SELECT * FROM conversation_reaction_receipts "
                "WHERE source_ref_json=? AND character_id=? AND reaction_version=?", self._key(work)).fetchone()
            if row is not None:
                if row["state"] == "COMMITTED":
                    try:
                        result = json.loads(row["result_json"])
                        if not isinstance(result, dict):
                            raise ValueError("invalid result")
                    except (ValueError, TypeError) as exc:
                        raise ExecutionUnavailable("CORRUPT_RESULT") from exc
                    return ReactionClaim(source, character_id, version, None, result)
                expired_preparation = (row["state"] == "PREPARING"
                    and row["preparation_deadline"] is not None
                    and row["preparation_deadline"] <= now)
                if row["state"] != "FAILED_BEFORE_CALL" and not expired_preparation:
                    raise ExecutionUnavailable(row["state"])
                self.store.conn.execute("UPDATE conversation_reaction_receipts SET state='PREPARING', "
                    "owner_token=?,error_kind=NULL,preparation_deadline=? "
                    "WHERE source_ref_json=? AND character_id=? AND reaction_version=?",
                    (work.token, now + self.preparation_lease_seconds, *self._key(work)))
            else:
                if legacy_check is not None and legacy_check():
                    raise ExecutionUnavailable("LEGACY_TRACE_REQUIRES_RECONCILIATION")
                self.store.conn.execute("INSERT INTO conversation_reaction_receipts "
                    "(source_ref_json,character_id,reaction_version,state,owner_token,result_json,error_kind,preparation_deadline) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (*self._key(work), "PREPARING", work.token, None, None, now + self.preparation_lease_seconds))
        return work

    def begin_model(self, work):
        self.require_independent_boundary(self.store)
        with self.store.transaction(immediate=True):
            changed = self.store.conn.execute("UPDATE conversation_reaction_receipts SET state='CALLING' "
                "WHERE source_ref_json=? AND character_id=? AND reaction_version=? AND owner_token=? AND state='PREPARING' "
                "AND preparation_deadline > ?",
                (*self._key(work), work.token, self._now())).rowcount
            if changed != 1:
                raise ExecutionUnavailable("LOST_OWNER")

    def commit(self, work, result: dict):
        with self.store._lock:
            if self.store._tx_depth <= 0:
                raise RuntimeError("cognition and its receipt must share the caller transaction")
            changed = self.store.conn.execute("UPDATE conversation_reaction_receipts SET state='COMMITTED',result_json=? "
                "WHERE source_ref_json=? AND character_id=? AND reaction_version=? AND owner_token=? AND state='CALLING'",
                (json.dumps(result, ensure_ascii=False), *self._key(work), work.token)).rowcount
            if changed != 1:
                raise ExecutionUnavailable("LOST_OWNER")

    def fail(self, work, error, *, superseded=False):
        with self.store.transaction(immediate=True):
            self.store.conn.execute("UPDATE conversation_reaction_receipts SET state="
                "CASE WHEN ? THEN 'SUPERSEDED' WHEN state='PREPARING' THEN 'FAILED_BEFORE_CALL' ELSE 'UNKNOWN' END, "
                "error_kind=? WHERE source_ref_json=? AND character_id=? AND reaction_version=? "
                "AND owner_token=? AND state IN ('PREPARING','CALLING')",
                (int(superseded), type(error).__name__, *self._key(work), work.token))
