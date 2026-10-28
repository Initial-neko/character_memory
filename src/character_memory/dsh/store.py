"""Durable P13 state: subjects, keys, grants, receipts, nonces and audit.

Every mutation opens (or joins) an immediate SQLite transaction. The audit hash
chain is appended inside that same transaction, so a process crash can never
commit a business row without its audit record. The chain is global: operator
and DSH listeners share it through the database write lock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from character_memory.dsh.auth import (
    parse_auth_headers,
    signature_message,
    verify_signature,
)
from character_memory.dsh.constants import (
    ACTION_SCOPE,
    ALLOWED_ACTIONS,
    ALLOWED_SCOPES,
    AUDIT_GENESIS_HASH,
    CLOCK_SKEW_SECONDS,
    DshError,
    MAX_ACTIVE_KEYS,
    MAX_ACTIONS_LIMIT,
    MAX_GRANT_TTL_SECONDS,
    MAX_PUBLIC_FACTS,
    MAX_READS_LIMIT,
    NON_GRANTABLE_SCOPES,
    NONCE_TTL_SECONDS,
    PARTICIPATE_SCOPE,
    PUBLIC_FACT_KINDS,
)
from character_memory.storage.activities import canonical


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DshAuthContext:
    subject_id: str
    key_id: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DshStore:
    def __init__(self, store, *, clock=None):
        self.store = store
        self._clock = clock or utc_now
        store.apply_schema_migration("dsh/001-authorization", self._migrate, immediate=True)

    # ---------------------------------------------------------------- schema

    def _migrate(self) -> None:
        # ``executescript`` would implicitly COMMIT the surrounding immediate
        # transaction before the migration marker is written, letting two
        # processes run the same migration concurrently. Execute every
        # statement individually so the marker and DDL share one transaction.
        conn = self.store.conn
        statements = (
            "CREATE TABLE IF NOT EXISTS dsh_subjects("
            "subject_id TEXT PRIMARY KEY,"
            "display_name TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),"
            "created_at TEXT NOT NULL,"
            "revoked_at TEXT,"
            "revoked_reason TEXT NOT NULL DEFAULT '',"
            "version INTEGER NOT NULL CHECK(version>=1))",
            "CREATE TABLE IF NOT EXISTS dsh_keys("
            "subject_id TEXT NOT NULL REFERENCES dsh_subjects(subject_id),"
            "key_id TEXT NOT NULL,"
            "public_key TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),"
            "created_at TEXT NOT NULL,"
            "revoked_at TEXT,"
            "PRIMARY KEY(subject_id,key_id))",
            "CREATE UNIQUE INDEX IF NOT EXISTS dsh_active_public_key "
            "ON dsh_keys(public_key) WHERE status='ACTIVE'",
            "CREATE TABLE IF NOT EXISTS dsh_grants("
            "grant_id TEXT PRIMARY KEY,"
            "subject_id TEXT NOT NULL REFERENCES dsh_subjects(subject_id),"
            "activity_id TEXT NOT NULL REFERENCES activities(id),"
            "scopes_json TEXT NOT NULL,"
            "public_facts_json TEXT NOT NULL DEFAULT '[]',"
            "actions_json TEXT NOT NULL,"
            "expires_at TEXT NOT NULL,"
            "expires_at_epoch INTEGER NOT NULL,"
            "max_reads INTEGER NOT NULL CHECK(max_reads>=1),"
            "reads_used INTEGER NOT NULL DEFAULT 0 "
            "CHECK(reads_used>=0 AND reads_used<=max_reads),"
            "max_actions INTEGER NOT NULL CHECK(max_actions>=1),"
            "actions_used INTEGER NOT NULL DEFAULT 0 "
            "CHECK(actions_used>=0 AND actions_used<=max_actions),"
            "status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),"
            "version INTEGER NOT NULL CHECK(version>=1),"
            "created_at TEXT NOT NULL,"
            "revoked_at TEXT,"
            "revoked_reason TEXT NOT NULL DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS dsh_request_receipts("
            "grant_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "operation TEXT NOT NULL,"
            "outcome_json TEXT NOT NULL,"
            "consumed_reads INTEGER NOT NULL DEFAULT 0,"
            "consumed_actions INTEGER NOT NULL DEFAULT 0,"
            "created_at TEXT NOT NULL,"
            "PRIMARY KEY(grant_id,request_id))",
            "CREATE TABLE IF NOT EXISTS dsh_nonces("
            "subject_id TEXT NOT NULL,"
            "key_id TEXT NOT NULL,"
            "nonce TEXT NOT NULL,"
            "seen_at_epoch INTEGER NOT NULL,"
            "PRIMARY KEY(subject_id,key_id,nonce))",
            "CREATE TABLE IF NOT EXISTS dsh_audit_events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "at TEXT NOT NULL,"
            "at_epoch INTEGER NOT NULL,"
            "subject_id TEXT,"
            "grant_id TEXT,"
            "request_id TEXT,"
            "event_type TEXT NOT NULL,"
            "outcome TEXT NOT NULL,"
            "resource_type TEXT NOT NULL DEFAULT '',"
            "resource_id TEXT NOT NULL DEFAULT '',"
            "request_hash TEXT NOT NULL DEFAULT '',"
            "detail_json TEXT NOT NULL DEFAULT '{}',"
            "prev_hash TEXT NOT NULL,"
            "record_hash TEXT NOT NULL UNIQUE)",
            "CREATE INDEX IF NOT EXISTS idx_dsh_grants_subject ON dsh_grants(subject_id)",
            "CREATE INDEX IF NOT EXISTS idx_dsh_audit_subject "
            "ON dsh_audit_events(subject_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_dsh_audit_grant "
            "ON dsh_audit_events(grant_id,id)",
        )
        for statement in statements:
            conn.execute(statement)

    # ---------------------------------------------------------------- utils

    def now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    @staticmethod
    def effective_grant_status(grant: dict, subject: dict, now: datetime) -> str:
        if subject.get("status") != "ACTIVE":
            return "SUBJECT_REVOKED"
        if grant.get("status") == "REVOKED":
            return "REVOKED"
        expires_at = parse_utc(grant["expires_at"])
        if not now < expires_at:
            return "EXPIRED"
        return "ACTIVE"

    @staticmethod
    def _grant_from_row(row) -> dict | None:
        if row is None:
            return None
        return {
            "grant_id": str(row["grant_id"]),
            "subject_id": str(row["subject_id"]),
            "activity_id": str(row["activity_id"]),
            "scopes": json.loads(row["scopes_json"] or "[]"),
            "actions": json.loads(row["actions_json"] or "[]"),
            "public_facts": json.loads(row["public_facts_json"] or "[]"),
            "expires_at": str(row["expires_at"]),
            "expires_at_epoch": int(row["expires_at_epoch"]),
            "max_reads": int(row["max_reads"]),
            "reads_used": int(row["reads_used"]),
            "max_actions": int(row["max_actions"]),
            "actions_used": int(row["actions_used"]),
            "status": str(row["status"]),
            "version": int(row["version"]),
            "created_at": str(row["created_at"]),
            "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
        }

    @staticmethod
    def _subject_from_row(row) -> dict | None:
        if row is None:
            return None
        return {
            "subject_id": str(row["subject_id"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
            "version": int(row["version"]),
        }

    @staticmethod
    def _key_from_row(row) -> dict | None:
        if row is None:
            return None
        return {
            "subject_id": str(row["subject_id"]),
            "key_id": str(row["key_id"]),
            "public_key": str(row["public_key"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
        }

    def get_subject(self, subject_id: str) -> dict | None:
        with self.store.transaction():
            return self._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?", (subject_id,)).fetchone())

    def get_key(self, subject_id: str, key_id: str) -> dict | None:
        with self.store.transaction():
            return self._key_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_keys WHERE subject_id=? AND key_id=?",
                (subject_id, key_id)).fetchone())

    def get_grant(self, grant_id: str) -> dict | None:
        with self.store.transaction():
            return self._grant_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_grants WHERE grant_id=?", (grant_id,)).fetchone())

    def list_grants(self, subject_id: str) -> list[dict]:
        with self.store.transaction():
            rows = self.store.conn.execute(
                "SELECT * FROM dsh_grants WHERE subject_id=? ORDER BY created_at,rowid",
                (subject_id,)).fetchall()
            return [self._grant_from_row(row) for row in rows if row is not None]

    # ---------------------------------------------------------------- audit

    def append_audit(
        self,
        *,
        event_type: str,
        outcome: str,
        subject_id: str | None = None,
        grant_id: str | None = None,
        request_id: str | None = None,
        resource_type: str = "",
        resource_id: str = "",
        request_hash: str = "",
        detail: dict | None = None,
        at: datetime | None = None,
    ) -> int:
        """Append one audit row. The caller must hold the write transaction."""
        conn = self.store.conn
        last = conn.execute(
            "SELECT record_hash FROM dsh_audit_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = str(last["record_hash"]) if last is not None else AUDIT_GENESIS_HASH
        moment = at or self.now()
        epoch = int(moment.timestamp())
        detail_value = detail if isinstance(detail, dict) else {}
        payload = {
            "prev_hash": prev_hash,
            "at": iso_utc(moment),
            "at_epoch": epoch,
            "subject_id": subject_id,
            "grant_id": grant_id,
            "request_id": request_id,
            "event_type": event_type,
            "outcome": outcome,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_hash": request_hash,
            "detail_json": detail_value,
        }
        record_hash = _sha256(canonical(payload))
        cursor = conn.execute(
            "INSERT INTO dsh_audit_events("
            "at,at_epoch,subject_id,grant_id,request_id,event_type,outcome,"
            "resource_type,resource_id,request_hash,detail_json,prev_hash,record_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["at"], epoch, subject_id, grant_id, request_id, event_type,
                outcome, resource_type, resource_id, request_hash,
                canonical(detail_value), prev_hash, record_hash,
            ),
        )
        return int(cursor.lastrowid)

    def audit_in_new_transaction(self, **kwargs) -> int:
        try:
            with self.store.transaction(immediate=True):
                return self.append_audit(**kwargs)
        except sqlite3.Error as exc:
            raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed") from exc

    def query_audit(
        self, *, subject_id: str | None = None, grant_id: str | None = None,
        after_id: int = 0, limit: int = 50,
    ) -> list[dict]:
        if type(after_id) is not int or after_id < 0:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid after_id")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid limit")
        clauses = ["id>?"]
        params: list[Any] = [after_id]
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(subject_id)
        if grant_id is not None:
            clauses.append("grant_id=?")
            params.append(grant_id)
        with self.store.transaction():
            rows = self.store.conn.execute(
                "SELECT * FROM dsh_audit_events WHERE " + " AND ".join(clauses) +
                " ORDER BY id ASC LIMIT ?", (*params, limit)).fetchall()
        return [
            {
                "id": int(row["id"]),
                "at": str(row["at"]),
                "at_epoch": int(row["at_epoch"]),
                "subject_id": row["subject_id"],
                "grant_id": row["grant_id"],
                "request_id": row["request_id"],
                "event_type": str(row["event_type"]),
                "outcome": str(row["outcome"]),
                "resource_type": str(row["resource_type"]),
                "resource_id": str(row["resource_id"]),
                "request_hash": str(row["request_hash"]),
                "detail_json": json.loads(row["detail_json"] or "{}"),
                "prev_hash": str(row["prev_hash"]),
                "record_hash": str(row["record_hash"]),
            }
            for row in rows
        ]

    @staticmethod
    def _recompute_audit_hash(row, detail) -> str:
        payload = {
            "prev_hash": str(row["prev_hash"]),
            "at": str(row["at"]),
            "at_epoch": int(row["at_epoch"]),
            "subject_id": row["subject_id"],
            "grant_id": row["grant_id"],
            "request_id": row["request_id"],
            "event_type": str(row["event_type"]),
            "outcome": str(row["outcome"]),
            "resource_type": str(row["resource_type"]),
            "resource_id": str(row["resource_id"]),
            "request_hash": str(row["request_hash"]),
            "detail_json": detail,
        }
        return _sha256(canonical(payload))

    def verify_audit(self) -> dict:
        with self.store.transaction():
            rows = self.store.conn.execute(
                "SELECT * FROM dsh_audit_events ORDER BY id ASC").fetchall()
        expected_prev = AUDIT_GENESIS_HASH
        checked = 0
        for row in rows:
            checked += 1
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"ok": False, "checked": checked, "first_broken_id": int(row["id"])}
            if not isinstance(detail, dict):
                return {"ok": False, "checked": checked, "first_broken_id": int(row["id"])}
            stored_prev = str(row["prev_hash"])
            recomputed = self._recompute_audit_hash(row, detail)
            if stored_prev != expected_prev or recomputed != str(row["record_hash"]):
                return {"ok": False, "checked": checked, "first_broken_id": int(row["id"])}
            expected_prev = str(row["record_hash"])
        return {"ok": True, "checked": checked, "first_broken_id": None}

    # ------------------------------------------------------------- receipts

    def admin_receipt(self, request_id: str) -> dict | None:
        row = self.store.conn.execute(
            "SELECT * FROM dsh_request_receipts WHERE grant_id='' AND request_id=?",
            (request_id,)).fetchone()
        if row is None:
            return None
        return {
            "request_hash": str(row["request_hash"]),
            "operation": str(row["operation"]),
            "outcome": json.loads(row["outcome_json"]),
        }

    @staticmethod
    def put_receipt(conn, *, grant_id: str, request_id: str, request_hash: str,
                    operation: str, outcome: dict, consumed_reads: int = 0,
                    consumed_actions: int = 0, at: str) -> None:
        conn.execute(
            "INSERT INTO dsh_request_receipts("
            "grant_id,request_id,request_hash,operation,outcome_json,"
            "consumed_reads,consumed_actions,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (grant_id, request_id, request_hash, operation,
             canonical(outcome), consumed_reads, consumed_actions, at),
        )

    # ------------------------------------------------------- authentication

    def authenticate(self, headers, *, method: str, path_with_query: str,
                     body: bytes, grant_id: str) -> DshAuthContext:
        try:
            parsed = parse_auth_headers(headers, grant_id=grant_id)
        except DshError as exc:
            self._audit_auth_deny(None, None, "HEADER", exc)
            raise
        with self.store._lock:
            subject = self._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?",
                (parsed.subject_id,)).fetchone())
            key = self._key_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_keys WHERE subject_id=? AND key_id=?",
                (parsed.subject_id, parsed.key_id)).fetchone())
        if subject is None:
            self._audit_auth_deny(parsed.subject_id, None, "UNKNOWN_SUBJECT", None)
            raise DshError(401, "DSH_AUTHENTICATION_FAILED", "unknown subject")
        if key is None:
            self._audit_auth_deny(parsed.subject_id, None, "UNKNOWN_KEY", None)
            raise DshError(401, "DSH_KEY_REVOKED", "key does not belong to subject")
        message = signature_message(
            method=method, path_with_query=path_with_query, body=body,
            timestamp=parsed.timestamp_raw, nonce=parsed.nonce, grant_id=grant_id,
        )
        try:
            verify_signature(key["public_key"], parsed.signature, message)
        except DshError as exc:
            self._audit_auth_deny(parsed.subject_id, parsed.key_id, "SIGNATURE", exc)
            raise
        now = self.now()
        if abs((now - parsed.timestamp).total_seconds()) > CLOCK_SKEW_SECONDS:
            self._audit_auth_deny(
                parsed.subject_id, parsed.key_id, "CLOCK_SKEW",
                DshError(401, "DSH_CLOCK_SKEW", "timestamp outside window"))
            raise DshError(401, "DSH_CLOCK_SKEW", "timestamp outside window")
        now_epoch = int(now.timestamp())
        try:
            with self.store.transaction(immediate=True):
                nonce_row = self.store.conn.execute(
                    "SELECT seen_at_epoch FROM dsh_nonces "
                    "WHERE subject_id=? AND key_id=? AND nonce=?",
                    (parsed.subject_id, parsed.key_id, parsed.nonce)).fetchone()
                if nonce_row is not None:
                    if now_epoch - int(nonce_row["seen_at_epoch"]) < NONCE_TTL_SECONDS:
                        raise DshError(401, "DSH_NONCE_REPLAY", "nonce already used")
                    self.store.conn.execute(
                        "DELETE FROM dsh_nonces WHERE subject_id=? AND key_id=? AND nonce=?",
                        (parsed.subject_id, parsed.key_id, parsed.nonce))
                # Re-read status under the write lock. The pre-signature rows
                # supplied the public key, but a concurrent revoke must still
                # win before the nonce is consumed.
                fresh_key = self.store.conn.execute(
                    "SELECT status FROM dsh_keys WHERE subject_id=? AND key_id=?",
                    (parsed.subject_id, parsed.key_id)).fetchone()
                fresh_subject = self.store.conn.execute(
                    "SELECT status FROM dsh_subjects WHERE subject_id=?",
                    (parsed.subject_id,)).fetchone()
                if fresh_key is None or str(fresh_key["status"]) != "ACTIVE":
                    raise DshError(401, "DSH_KEY_REVOKED", "key revoked")
                if fresh_subject is None or str(fresh_subject["status"]) != "ACTIVE":
                    raise DshError(403, "DSH_SUBJECT_REVOKED", "subject revoked")
                self.store.conn.execute(
                    "DELETE FROM dsh_nonces WHERE seen_at_epoch<?",
                    (now_epoch - NONCE_TTL_SECONDS,))
                self.store.conn.execute(
                    "INSERT INTO dsh_nonces(subject_id,key_id,nonce,seen_at_epoch) "
                    "VALUES(?,?,?,?)",
                    (parsed.subject_id, parsed.key_id, parsed.nonce, now_epoch))
        except DshError as exc:
            self._audit_auth_deny(parsed.subject_id, parsed.key_id, exc.code, exc)
            raise
        return DshAuthContext(subject_id=parsed.subject_id, key_id=parsed.key_id)

    def _audit_auth_deny(self, subject_id: str | None, key_id: str | None,
                         reason: str, error: DshError | None) -> None:
        try:
            self.audit_in_new_transaction(
                event_type="AUTH_DENY",
                outcome="DENY",
                subject_id=subject_id,
                detail={"reason": reason, "code": error.code if error else reason,
                        "key_id": key_id},
            )
        except DshError:
            raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed")

    # ---------------------------------------------------------- registration

    def register_subject(self, *, request_id: str, display_name: str,
                         public_key: str) -> dict:
        from character_memory.dsh.auth import validate_public_key

        name = str(display_name or "").strip()
        if not 1 <= len(name) <= 60:
            raise DshError(422, "DSH_SCHEMA_INVALID", "display_name must be 1-60 characters")
        try:
            canonical_key = validate_public_key(public_key)
        except (ValueError, TypeError) as exc:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid Ed25519 public key") from exc
        operation = "ADMIN_SUBJECT_REGISTER"
        request_hash = _sha256(canonical({
            "operation": operation,
            "payload": {"display_name": name, "public_key": canonical_key},
        }))
        with self.store.transaction(immediate=True):
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            now = self.now()
            subject_id = "dsh_" + uuid4().hex
            key_id = "key_" + uuid4().hex[:16]
            try:
                self.store.conn.execute(
                    "INSERT INTO dsh_subjects("
                    "subject_id,display_name,status,created_at,version) "
                    "VALUES(?,?,'ACTIVE',?,1)",
                    (subject_id, name, iso_utc(now)),
                )
                self.store.conn.execute(
                    "INSERT INTO dsh_keys("
                    "subject_id,key_id,public_key,status,created_at) "
                    "VALUES(?,?,?,'ACTIVE',?)",
                    (subject_id, key_id, canonical_key, iso_utc(now)),
                )
            except sqlite3.IntegrityError as exc:
                raise DshError(422, "DSH_SCHEMA_INVALID",
                               "public key already active") from exc
            outcome = {
                "subject_id": subject_id,
                "key_id": key_id,
                "status": "ACTIVE",
                "created_at": iso_utc(now),
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="SUBJECT_REGISTER", outcome="OK", subject_id=subject_id,
                resource_type="SUBJECT", resource_id=subject_id,
                detail={"key_id": key_id, "request_id": request_id}, at=now)
            return outcome

    def add_key(self, *, subject_id: str, request_id: str, public_key: str) -> dict:
        from character_memory.dsh.auth import validate_public_key

        try:
            canonical_key = validate_public_key(public_key)
        except (ValueError, TypeError) as exc:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid Ed25519 public key") from exc
        operation = "ADMIN_KEY_ADD"
        request_hash = _sha256(canonical({
            "operation": operation,
            "payload": {"subject_id": subject_id, "public_key": canonical_key},
        }))
        with self.store.transaction(immediate=True):
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            subject = self._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?", (subject_id,)).fetchone())
            if subject is None:
                raise DshError(404, "DSH_SUBJECT_NOT_FOUND", "unknown subject")
            if subject["status"] != "ACTIVE":
                raise DshError(403, "DSH_SUBJECT_REVOKED", "subject revoked")
            active = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM dsh_keys WHERE subject_id=? AND status='ACTIVE'",
                (subject_id,)).fetchone()
            if int(active["n"]) >= MAX_ACTIVE_KEYS:
                raise DshError(409, "DSH_KEY_LIMIT", "at most two active keys")
            now = self.now()
            key_id = "key_" + uuid4().hex[:16]
            try:
                self.store.conn.execute(
                    "INSERT INTO dsh_keys("
                    "subject_id,key_id,public_key,status,created_at) "
                    "VALUES(?,?,?,'ACTIVE',?)",
                    (subject_id, key_id, canonical_key, iso_utc(now)))
            except sqlite3.IntegrityError as exc:
                raise DshError(422, "DSH_SCHEMA_INVALID",
                               "public key already active") from exc
            outcome = {
                "subject_id": subject_id,
                "key_id": key_id,
                "status": "ACTIVE",
                "created_at": iso_utc(now),
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="KEY_ADD", outcome="OK", subject_id=subject_id,
                resource_type="KEY", resource_id=key_id,
                detail={"request_id": request_id}, at=now)
            return outcome

    def revoke_key(self, *, subject_id: str, key_id: str, request_id: str) -> dict:
        operation = "ADMIN_KEY_REVOKE"
        request_hash = _sha256(canonical({
            "operation": operation,
            "payload": {"subject_id": subject_id, "key_id": key_id},
        }))
        with self.store.transaction(immediate=True):
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            row = self.store.conn.execute(
                "SELECT * FROM dsh_keys WHERE subject_id=? AND key_id=?",
                (subject_id, key_id)).fetchone()
            if row is None:
                raise DshError(404, "DSH_KEY_NOT_FOUND", "unknown key")
            now = self.now()
            already = str(row["status"]) == "REVOKED"
            if not already:
                self.store.conn.execute(
                    "UPDATE dsh_keys SET status='REVOKED',revoked_at=? "
                    "WHERE subject_id=? AND key_id=?",
                    (iso_utc(now), subject_id, key_id))
            outcome = {
                "subject_id": subject_id,
                "key_id": key_id,
                "status": "REVOKED",
                "already_revoked": already,
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="KEY_REVOKE", outcome="OK", subject_id=subject_id,
                resource_type="KEY", resource_id=key_id,
                detail={"request_id": request_id, "already_revoked": already}, at=now)
            return outcome

    def revoke_subject(self, *, subject_id: str, request_id: str) -> dict:
        operation = "ADMIN_SUBJECT_REVOKE"
        request_hash = _sha256(canonical({
            "operation": operation, "payload": {"subject_id": subject_id}}))
        with self.store.transaction(immediate=True):
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            subject = self._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?", (subject_id,)).fetchone())
            if subject is None:
                raise DshError(404, "DSH_SUBJECT_NOT_FOUND", "unknown subject")
            now = self.now()
            already = subject["status"] == "REVOKED"
            affected: list[str] = []
            if not already:
                self.store.conn.execute(
                    "UPDATE dsh_subjects SET status='REVOKED',revoked_at=?,"
                    "revoked_reason=?,version=version+1 WHERE subject_id=?",
                    (iso_utc(now), "subject revoked", subject_id))
                rows = self.store.conn.execute(
                    "SELECT grant_id FROM dsh_grants WHERE subject_id=? AND status='ACTIVE'",
                    (subject_id,)).fetchall()
                affected = [str(row["grant_id"]) for row in rows]
                self.store.conn.execute(
                    "UPDATE dsh_grants SET status='REVOKED',revoked_at=?,"
                    "revoked_reason=?,version=version+1 "
                    "WHERE subject_id=? AND status='ACTIVE'",
                    (iso_utc(now), "subject revoked", subject_id))
            outcome = {
                "subject_id": subject_id,
                "status": "REVOKED",
                "already_revoked": already,
                "revoked_grants": affected,
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="SUBJECT_REVOKE", outcome="OK", subject_id=subject_id,
                resource_type="SUBJECT", resource_id=subject_id,
                detail={"request_id": request_id, "already_revoked": already,
                        "revoked_grants": affected}, at=now)
            return outcome

    # --------------------------------------------------------------- grants

    @staticmethod
    def validate_grant_spec(spec: dict, now: datetime) -> dict:
        scopes = spec.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            raise DshError(422, "DSH_SCHEMA_INVALID", "scopes must not be empty")
        normalized_scopes: list[str] = []
        for scope in scopes:
            if not isinstance(scope, str):
                raise DshError(422, "DSH_SCHEMA_INVALID", "scope must be a string")
            if scope == "*" or scope in NON_GRANTABLE_SCOPES:
                raise DshError(422, "DSH_SCOPE_NOT_GRANTABLE", f"scope not grantable: {scope}")
            if scope not in ALLOWED_SCOPES:
                raise DshError(422, "DSH_SCOPE_UNKNOWN", f"unknown scope: {scope}")
            if scope not in normalized_scopes:
                normalized_scopes.append(scope)
        if PARTICIPATE_SCOPE in normalized_scopes and "ACTIVITY_READ_SELF" not in normalized_scopes:
            raise DshError(422, "DSH_SCOPE_DEPENDENCY",
                           "ACTIVITY_PARTICIPATE requires ACTIVITY_READ_SELF")
        actions = spec.get("actions", [])
        if not isinstance(actions, list):
            raise DshError(422, "DSH_SCHEMA_INVALID", "actions must be a list")
        normalized_actions: list[str] = []
        for action in actions:
            if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
                raise DshError(422, "DSH_SCOPE_NOT_GRANTABLE",
                               f"action not grantable: {action}")
            if ACTION_SCOPE[action] not in normalized_scopes:
                raise DshError(422, "DSH_SCOPE_NOT_GRANTABLE",
                               f"action requires scope {ACTION_SCOPE[action]}")
            if action not in normalized_actions:
                normalized_actions.append(action)
        public_facts = spec.get("public_facts", [])
        if not isinstance(public_facts, list) or len(public_facts) > MAX_PUBLIC_FACTS:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid public_facts")
        normalized_facts: list[dict] = []
        for item in public_facts:
            if not isinstance(item, dict):
                raise DshError(422, "DSH_SCHEMA_INVALID", "invalid public_fact")
            kind = str(item.get("kind") or "").strip().upper()
            fact_id = item.get("id")
            if kind not in PUBLIC_FACT_KINDS:
                raise DshError(422, "DSH_SCHEMA_INVALID", "unsupported public_fact kind")
            if type(fact_id) is not int or fact_id < 1:
                raise DshError(422, "DSH_SCHEMA_INVALID", "public_fact id must be a positive integer")
            entry = {"kind": kind, "id": fact_id}
            if entry not in normalized_facts:
                normalized_facts.append(entry)
        max_reads = spec.get("max_reads", 200)
        max_actions = spec.get("max_actions", 20)
        if type(max_reads) is not int or not 1 <= max_reads <= MAX_READS_LIMIT:
            raise DshError(422, "DSH_SCHEMA_INVALID", "max_reads must be 1-1000")
        if type(max_actions) is not int or not 1 <= max_actions <= MAX_ACTIONS_LIMIT:
            raise DshError(422, "DSH_SCHEMA_INVALID", "max_actions must be 1-100")
        if "expires_at" not in spec or spec.get("expires_at") in (None, ""):
            raise DshError(422, "DSH_SCHEMA_INVALID", "expires_at is required")
        try:
            expires_at = parse_utc(spec["expires_at"])
        except (TypeError, ValueError) as exc:
            raise DshError(422, "DSH_SCHEMA_INVALID", "expires_at must be UTC") from exc
        if not now < expires_at:
            raise DshError(422, "DSH_SCHEMA_INVALID", "expires_at must be in the future")
        if expires_at > now + timedelta(seconds=MAX_GRANT_TTL_SECONDS):
            raise DshError(422, "DSH_SCHEMA_INVALID", "expires_at exceeds 7 days")
        return {
            "scopes": normalized_scopes,
            "actions": normalized_actions,
            "public_facts": normalized_facts,
            "expires_at": expires_at,
            "max_reads": max_reads,
            "max_actions": max_actions,
        }

    def create_grant(self, *, request_id: str, subject_id: str, activity_id: str,
                     spec: dict) -> dict:
        with self.store.transaction(immediate=True):
            now = self.now()
            normalized = self.validate_grant_spec(spec, now)
            operation = "ADMIN_GRANT_CREATE"
            payload = {
                "subject_id": subject_id,
                "activity_id": activity_id,
                "scopes": normalized["scopes"],
                "actions": normalized["actions"],
                "public_facts": normalized["public_facts"],
                "expires_at": iso_utc(normalized["expires_at"]),
                "max_reads": normalized["max_reads"],
                "max_actions": normalized["max_actions"],
            }
            request_hash = _sha256(canonical({"operation": operation, "payload": payload}))
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            subject = self._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?", (subject_id,)).fetchone())
            if subject is None or subject["status"] != "ACTIVE":
                raise DshError(403, "DSH_SUBJECT_REVOKED", "subject is not active")
            activity = self.store.conn.execute(
                "SELECT id FROM activities WHERE id=?", (activity_id,)).fetchone()
            if activity is None:
                raise DshError(404, "ACTIVITY_NOT_FOUND", "unknown activity")
            participant = self.store.conn.execute(
                "SELECT participation_status FROM activity_participants "
                "WHERE activity_id=? AND participant_key=?",
                (activity_id, f"dsh:{subject_id}")).fetchone()
            if participant is None or str(participant["participation_status"]) != "ACTIVE":
                raise DshError(422, "DSH_PARTICIPANT_MISSING",
                               "subject is not an active participant of this activity")
            grant_id = "grant_" + uuid4().hex
            self.store.conn.execute(
                "INSERT INTO dsh_grants("
                "grant_id,subject_id,activity_id,scopes_json,public_facts_json,"
                "actions_json,expires_at,expires_at_epoch,max_reads,max_actions,"
                "status,version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'ACTIVE',1,?)",
                (
                    grant_id, subject_id, activity_id,
                    canonical(normalized["scopes"]),
                    canonical(normalized["public_facts"]),
                    canonical(normalized["actions"]),
                    iso_utc(normalized["expires_at"]),
                    int(normalized["expires_at"].timestamp()),
                    normalized["max_reads"], normalized["max_actions"],
                    iso_utc(now),
                ),
            )
            outcome = {
                "grant_id": grant_id,
                "subject_id": subject_id,
                "activity_id": activity_id,
                "scopes": normalized["scopes"],
                "actions": normalized["actions"],
                "public_facts": normalized["public_facts"],
                "expires_at": iso_utc(normalized["expires_at"]),
                "max_reads": normalized["max_reads"],
                "max_actions": normalized["max_actions"],
                "status": "ACTIVE",
                "version": 1,
                "created_at": iso_utc(now),
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="GRANT_CREATE", outcome="OK", subject_id=subject_id,
                grant_id=grant_id, resource_type="GRANT", resource_id=grant_id,
                detail={"request_id": request_id, "activity_id": activity_id,
                        "scopes": normalized["scopes"], "actions": normalized["actions"],
                        "expires_at": iso_utc(normalized["expires_at"])},
                at=now)
            return outcome

    def revoke_grant(self, *, grant_id: str, request_id: str) -> dict:
        operation = "ADMIN_GRANT_REVOKE"
        request_hash = _sha256(canonical({
            "operation": operation, "payload": {"grant_id": grant_id}}))
        with self.store.transaction(immediate=True):
            replay = self.admin_receipt(request_id)
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                return {**replay["outcome"], "replayed": True}
            row = self.store.conn.execute(
                "SELECT * FROM dsh_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                raise DshError(403, "DSH_GRANT_MISSING", "unknown grant")
            now = self.now()
            already = str(row["status"]) == "REVOKED"
            if not already:
                self.store.conn.execute(
                    "UPDATE dsh_grants SET status='REVOKED',revoked_at=?,"
                    "revoked_reason=?,version=version+1 "
                    "WHERE grant_id=? AND status='ACTIVE'",
                    (iso_utc(now), "grant revoked", grant_id))
            outcome = {
                "grant_id": grant_id,
                "status": "REVOKED",
                "already_revoked": already,
            }
            self.put_receipt(
                self.store.conn, grant_id="", request_id=request_id,
                request_hash=request_hash, operation=operation, outcome=outcome,
                at=iso_utc(now))
            self.append_audit(
                event_type="GRANT_REVOKE", outcome="OK",
                subject_id=str(row["subject_id"]), grant_id=grant_id,
                resource_type="GRANT", resource_id=grant_id,
                detail={"request_id": request_id, "already_revoked": already}, at=now)
            return outcome
