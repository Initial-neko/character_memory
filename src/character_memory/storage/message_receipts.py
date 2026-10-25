"""Durable acceptance receipts, referencing existing authoritative message facts.

This records acceptance and last confirmed queue handoff, not model completion.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json

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
                    fact = self.store.get_event(ref.id)
                    valid = fact is not None and fact.character_id == scope[1]
                else:
                    valid = self.store.conn.execute("SELECT 1 FROM conversation_events WHERE id=? AND conversation_id=?",
                        (ref.id, ref.conversation_id)).fetchone() is not None
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
