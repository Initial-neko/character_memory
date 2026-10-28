"""P13 application service: typed Activity participation through public stores.

The DSH surface never writes SQL from a route. This module owns the judgment
order from the frozen contract, the quota mutation, the fact insert and the
receipt; each action is one immediate transaction. Reads use the same grant,
receipt and audit rules but are side-effect free apart from quota and audit.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Callable

from character_memory.domain.activity import (
    activity_experience_scope,
    dsh_participant_id,
)
from character_memory.dsh.constants import (
    ACTION_LEAVE,
    ACTION_SUBMIT_TURN,
    ALLOWED_ACTIONS,
    DshError,
)
from character_memory.dsh.store import DshAuthContext, DshStore, iso_utc
from character_memory.storage.activities import (
    ActivityCorrupt,
    ActivityMissing,
    ActivityRepository,
    canonical,
)
from character_memory.storage.activity_facts import (
    ActivityFactConflict,
    ActivityFactCorrupt,
    ActivityFactRepository,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DshService:
    def __init__(self, store, *, clock=None, write_guard: Callable[[], None] | None = None,
                 profile_lookup: Callable[[str], dict] | None = None):
        self.store = store
        self.dsh = DshStore(store, clock=clock)
        self.activities = ActivityRepository(store)
        self.facts = ActivityFactRepository(store)
        self._write_guard = write_guard
        self._profile_lookup = profile_lookup
        self._space = None
        self._space_media = None
        self._space_media_import_failed = False

    # ------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return self.dsh.now()

    def _grant_rows(self, grant_id: str) -> tuple[dict, dict]:
        grant_row = self.store.conn.execute(
            "SELECT * FROM dsh_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if grant_row is None:
            raise DshError(403, "DSH_GRANT_MISSING", "grant not found")
        grant = DshStore._grant_from_row(grant_row)
        subject = DshStore._subject_from_row(self.store.conn.execute(
            "SELECT * FROM dsh_subjects WHERE subject_id=?",
            (grant["subject_id"],)).fetchone())
        if subject is None:
            raise DshError(403, "DSH_GRANT_MISSING", "grant subject not found")
        return grant, subject

    def _authorize(self, ctx: DshAuthContext, grant_id: str, *,
                   scope: str, now: datetime) -> tuple[dict, dict]:
        grant, subject = self._grant_rows(grant_id)
        if grant["subject_id"] != ctx.subject_id:
            raise DshError(403, "DSH_GRANT_MISSING", "grant does not belong to subject")
        status = DshStore.effective_grant_status(grant, subject, now)
        if status == "SUBJECT_REVOKED":
            raise DshError(403, "DSH_SUBJECT_REVOKED", "subject revoked")
        if status == "REVOKED":
            raise DshError(403, "DSH_GRANT_REVOKED", "grant revoked")
        if status == "EXPIRED":
            raise DshError(403, "DSH_GRANT_EXPIRED", "grant expired")
        if scope not in grant["scopes"]:
            raise DshError(403, "DSH_SCOPE_DENIED", "scope not granted")
        return grant, subject

    @staticmethod
    def _check_activity(grant: dict, requested_activity_id: str | None) -> None:
        if requested_activity_id is not None and requested_activity_id != grant["activity_id"]:
            raise DshError(403, "DSH_ACTIVITY_SCOPE_DENIED",
                           "grant is bound to a different activity")

    def _participant_row(self, activity_id: str, subject_id: str) -> dict | None:
        return self.activities.get_participant(
            activity_id, dsh_participant_id(subject_id))

    def _activity(self, activity_id: str) -> dict:
        try:
            return self.activities.get(activity_id)
        except ActivityMissing as exc:
            raise DshError(404, "ACTIVITY_NOT_FOUND", "activity not found") from exc
        except ActivityCorrupt as exc:
            raise DshError(503, "ACTIVITY_CORRUPT", "activity corrupt") from exc

    def _fact_projection(self, fact: dict, activity_id: str, display_name: str) -> dict:
        expected = activity_experience_scope(activity_id)
        scope = fact.get("experience_scope")
        if not isinstance(scope, dict) or scope != expected:
            raise DshError(503, "DSH_SCOPE_BROKEN", "stored fact scope is missing or invalid")
        if str(fact.get("actor_type")) != "DSH":
            raise DshError(503, "DSH_SCOPE_BROKEN", "unexpected fact actor")
        return {
            "fact_id": fact["fact_id"],
            "seq": fact["seq"],
            "actor": {"type": "DSH",
                      "subject_id": fact["authorization"].get("subject_id"),
                      "display_name": display_name},
            "content": fact["content"],
            "reply_to_fact_id": fact["reply_to_fact_id"],
            "experience_scope": scope,
            "created_at": fact["created_at"],
        }

    def _display_name(self, subject_id: str) -> str:
        subject = self.dsh.get_subject(subject_id)
        return str(subject["display_name"]) if subject else subject_id

    # -------------------------------------------------------------- receipts

    def _receipt(self, grant_id: str, request_id: str) -> dict | None:
        row = self.store.conn.execute(
            "SELECT * FROM dsh_request_receipts WHERE grant_id=? AND request_id=?",
            (grant_id, request_id)).fetchone()
        if row is None:
            return None
        return {
            "request_hash": str(row["request_hash"]),
            "operation": str(row["operation"]),
            "outcome": json.loads(row["outcome_json"]),
        }

    def _deny_read(self, ctx: DshAuthContext, grant_id: str, request_id: str,
                   exc: DshError, *, event_type: str = "READ") -> None:
        try:
            self.dsh.audit_in_new_transaction(
                event_type=event_type, outcome="DENY", subject_id=ctx.subject_id,
                grant_id=grant_id, request_id=request_id,
                resource_type="GRANT", resource_id=grant_id,
                detail={"code": exc.code}, request_hash="")
        except DshError:
            raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed") from exc
        exc.audited = True

    def _deny_action(self, ctx: DshAuthContext, grant_id: str, request_id: str,
                     request_hash: str, exc: DshError) -> None:
        try:
            self.dsh.audit_in_new_transaction(
                event_type="ACTION_DENY", outcome="DENY", subject_id=ctx.subject_id,
                grant_id=grant_id, request_id=request_id,
                resource_type="GRANT", resource_id=grant_id,
                request_hash=request_hash, detail={"code": exc.code})
        except DshError:
            raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed") from exc
        exc.audited = True

    # ---------------------------------------------------------------- reads

    def read_self(self, ctx: DshAuthContext, request_id: str) -> dict:
        with self.store.transaction(immediate=True):
            subject = self.dsh._subject_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_subjects WHERE subject_id=?", (ctx.subject_id,)).fetchone())
            key = self.dsh._key_from_row(self.store.conn.execute(
                "SELECT * FROM dsh_keys WHERE subject_id=? AND key_id=?",
                (ctx.subject_id, ctx.key_id)).fetchone())
            if subject is None or key is None:
                raise DshError(401, "DSH_AUTHENTICATION_FAILED", "identity disappeared")
            now = self._now()
            grants = []
            for row in self.store.conn.execute(
                    "SELECT * FROM dsh_grants WHERE subject_id=? ORDER BY created_at,rowid",
                    (ctx.subject_id,)).fetchall():
                grant = DshStore._grant_from_row(row)
                grants.append({
                    "grant_id": grant["grant_id"],
                    "activity_id": grant["activity_id"],
                    "scopes": grant["scopes"],
                    "actions": grant["actions"],
                    "expires_at": grant["expires_at"],
                    "effective_status": DshStore.effective_grant_status(grant, subject, now),
                    "reads_used": grant["reads_used"],
                    "max_reads": grant["max_reads"],
                    "actions_used": grant["actions_used"],
                    "max_actions": grant["max_actions"],
                })
            self.dsh.append_audit(
                event_type="READ", outcome="OK", subject_id=ctx.subject_id,
                request_id=request_id, resource_type="SUBJECT",
                resource_id=ctx.subject_id, detail={"resource": "SELF"})
            return {
                "subject": {k: subject[k] for k in
                            ("subject_id", "display_name", "status", "created_at", "version")},
                "key": {k: key[k] for k in ("key_id", "status", "created_at")},
                "grants": grants,
            }

    def read_activity(self, ctx: DshAuthContext, grant_id: str, request_id: str,
                      *, requested_activity_id: str | None = None) -> dict:
        request_hash = _sha256(canonical(
            {"operation": "READ_ACTIVITY", "grant_id": grant_id, "request_id": request_id}))
        try:
            with self.store.transaction(immediate=True):
                now = self._now()
                grant, _subject = self._authorize(
                    ctx, grant_id, scope="ACTIVITY_READ_SELF", now=now)
                self._check_activity(grant, requested_activity_id)
                receipt = self._receipt(grant_id, request_id)
                if receipt is not None:
                    if receipt["request_hash"] != request_hash:
                        raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                    activity = self._activity(grant["activity_id"])
                    self.dsh.append_audit(
                        event_type="READ_REPLAY", outcome="REPLAY",
                        subject_id=ctx.subject_id, grant_id=grant_id,
                        request_id=request_id, resource_type="ACTIVITY",
                        resource_id=grant["activity_id"], request_hash=request_hash,
                        detail={"resource": "ACTIVITY"})
                    return {**activity, "replayed": True}
                if grant["reads_used"] >= grant["max_reads"]:
                    raise DshError(403, "DSH_QUOTA_EXHAUSTED", "read quota exhausted")
                activity = self._activity(grant["activity_id"])
                self.store.conn.execute(
                    "UPDATE dsh_grants SET reads_used=reads_used+1 WHERE grant_id=?",
                    (grant_id,))
                self.dsh.put_receipt(
                    self.store.conn, grant_id=grant_id, request_id=request_id,
                    request_hash=request_hash, operation="READ_ACTIVITY",
                    outcome={"activity_id": grant["activity_id"]},
                    consumed_reads=1, at=iso_utc(now))
                self.dsh.append_audit(
                    event_type="READ", outcome="OK", subject_id=ctx.subject_id,
                    grant_id=grant_id, request_id=request_id,
                    resource_type="ACTIVITY", resource_id=grant["activity_id"],
                    request_hash=request_hash,
                    detail={"resource": "ACTIVITY", "reads_used": grant["reads_used"] + 1})
                return {**activity, "replayed": False}
        except DshError as exc:
            if exc.status < 500:
                self._deny_read(ctx, grant_id, request_id, exc)
            raise

    def read_transcript(self, ctx: DshAuthContext, grant_id: str, request_id: str,
                        *, after_seq: int = 0, limit: int = 50,
                        requested_activity_id: str | None = None) -> dict:
        if type(after_seq) is not int or after_seq < 0:
            raise DshError(422, "DSH_SCHEMA_INVALID", "after_seq must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DshError(422, "DSH_SCHEMA_INVALID", "limit must be 1-50")
        request_hash = _sha256(canonical({
            "operation": "READ_TRANSCRIPT", "grant_id": grant_id,
            "request_id": request_id, "after_seq": after_seq, "limit": limit,
        }))
        try:
            with self.store.transaction(immediate=True):
                now = self._now()
                grant, _subject = self._authorize(
                    ctx, grant_id, scope="ACTIVITY_READ_TRANSCRIPT", now=now)
                self._check_activity(grant, requested_activity_id)
                receipt = self._receipt(grant_id, request_id)
                replayed = receipt is not None
                if receipt is not None and receipt["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                if not replayed:
                    if grant["reads_used"] >= grant["max_reads"]:
                        raise DshError(403, "DSH_QUOTA_EXHAUSTED", "read quota exhausted")
                    self.store.conn.execute(
                        "UPDATE dsh_grants SET reads_used=reads_used+1 WHERE grant_id=?",
                        (grant_id,))
                try:
                    rows = self.facts.list(grant["activity_id"], after_seq=after_seq, limit=limit)
                except (ActivityFactCorrupt, ActivityFactConflict) as exc:
                    raise DshError(503, "DSH_SCOPE_BROKEN", "fact scope is missing or invalid") from exc
                display_name = self._display_name(ctx.subject_id)
                facts = [self._fact_projection(fact, grant["activity_id"], display_name)
                         for fact in rows]
                if not replayed:
                    self.dsh.put_receipt(
                        self.store.conn, grant_id=grant_id, request_id=request_id,
                        request_hash=request_hash, operation="READ_TRANSCRIPT",
                        outcome={"activity_id": grant["activity_id"]}, consumed_reads=1,
                        at=iso_utc(now))
                self.dsh.append_audit(
                    event_type="READ_REPLAY" if replayed else "READ",
                    outcome="REPLAY" if replayed else "OK",
                    subject_id=ctx.subject_id, grant_id=grant_id, request_id=request_id,
                    resource_type="ACTIVITY", resource_id=grant["activity_id"],
                    request_hash=request_hash,
                    detail={"resource": "TRANSCRIPT", "after_seq": after_seq,
                            "count": len(facts),
                            "reads_used": grant["reads_used"] + (0 if replayed else 1)})
                return {
                    "activity_id": grant["activity_id"],
                    "facts": facts,
                    "replayed": replayed,
                }
        except DshError as exc:
            if exc.status < 500:
                self._deny_read(ctx, grant_id, request_id, exc)
            raise

    def read_space_post(self, ctx: DshAuthContext, grant_id: str, post_id: int,
                        request_id: str, *,
                        requested_activity_id: str | None = None) -> dict:
        if type(post_id) is not int or post_id < 1:
            raise DshError(422, "DSH_SCHEMA_INVALID", "invalid post id")
        request_hash = _sha256(canonical({
            "operation": "READ_SPACE_POST", "grant_id": grant_id,
            "request_id": request_id, "post_id": post_id,
        }))
        try:
            with self.store.transaction(immediate=True):
                now = self._now()
                grant, _subject = self._authorize(
                    ctx, grant_id, scope="SPACE_POST_READ", now=now)
                self._check_activity(grant, requested_activity_id)
                listed = any(item.get("kind") == "SPACE_POST" and item.get("id") == post_id
                             for item in grant["public_facts"])
                if not listed:
                    raise DshError(403, "DSH_SCOPE_DENIED", "post is not in public_facts")
                receipt = self._receipt(grant_id, request_id)
                replayed = receipt is not None
                if receipt is not None and receipt["request_hash"] != request_hash:
                    raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                if not replayed:
                    if grant["reads_used"] >= grant["max_reads"]:
                        raise DshError(403, "DSH_QUOTA_EXHAUSTED", "read quota exhausted")
                if self._space is None:
                    from character_memory.space_store import SpaceRepository
                    self._space = SpaceRepository(self.store)
                post = self._space.get_post(post_id)
                if post is None or post.visibility != "ACTIVE_CHARACTERS":
                    raise DshError(404, "DSH_FACT_NOT_FOUND", "post not found")
                payload = self._space_payload(post)
                if not replayed:
                    self.store.conn.execute(
                        "UPDATE dsh_grants SET reads_used=reads_used+1 WHERE grant_id=?",
                        (grant_id,))
                    self.dsh.put_receipt(
                        self.store.conn, grant_id=grant_id, request_id=request_id,
                        request_hash=request_hash, operation="READ_SPACE_POST",
                        outcome={"post_id": post_id}, consumed_reads=1,
                        at=iso_utc(now))
                self.dsh.append_audit(
                    event_type="READ_REPLAY" if replayed else "READ",
                    outcome="REPLAY" if replayed else "OK",
                    subject_id=ctx.subject_id, grant_id=grant_id, request_id=request_id,
                    resource_type="SPACE_POST", resource_id=str(post_id),
                    request_hash=request_hash,
                    detail={"resource": "SPACE_POST", "post_id": post_id,
                            "reads_used": grant["reads_used"] + (0 if replayed else 1)})
                return {**payload, "replayed": replayed}
        except DshError as exc:
            if exc.status < 500:
                self._deny_read(ctx, grant_id, request_id, exc)
            raise

    def _space_payload(self, post) -> dict:
        profile = {"id": post.character_id, "name": post.character_id,
                   "identity": "", "tagline": "", "avatar_url": ""}
        if self._profile_lookup is not None:
            try:
                candidate = self._profile_lookup(post.character_id) or {}
            except Exception:
                candidate = {}
            profile = {
                "id": post.character_id,
                "name": str(candidate.get("name") or post.character_id),
                "identity": str(candidate.get("identity") or ""),
                "tagline": str(candidate.get("tagline") or ""),
                "avatar_url": str(candidate.get("avatar_url") or ""),
            }
        media_items = self._media_items(post)
        scope = post.experience_scope
        scope_status = "LEGACY_REAL" if not scope else str(scope.get("reality") or "UNKNOWN")
        payload = {
            "id": post.id,
            "character_id": post.character_id,
            "author": profile,
            "content": post.content,
            "created_at": post.created_at.isoformat(),
            "visibility": post.visibility,
            "media_items": media_items,
            "experience_scope": scope,
            "scope_status": scope_status,
        }
        return payload

    def _media_items(self, post) -> list[dict]:
        if self._space_media is None and not self._space_media_import_failed:
            try:
                from character_memory.space_media import SpacePostMediaRepository
                self._space_media = SpacePostMediaRepository(self.store)
            except Exception:
                self._space_media_import_failed = True
        if self._space_media is not None:
            try:
                relations = self._space_media.list_for_post(post.id)
            except Exception:
                relations = []
            if relations:
                return [{
                    "id": item.media_id,
                    "media_id": item.media_id,
                    "media_type": item.media_type,
                    "sort_order": item.sort_order,
                    "available": True,
                } for item in relations]
        if post.media_id:
            return [{"id": post.media_id, "media_id": post.media_id,
                     "media_type": "LEGACY", "sort_order": 0, "available": True}]
        return []

    # -------------------------------------------------------------- actions

    @staticmethod
    def action_request_hash(grant_id: str, action: dict) -> str:
        return _sha256(canonical({
            "operation": "DSH_ACTION",
            "grant_id": grant_id,
            "type": action["type"],
            "request_id": action["request_id"],
            "content": action.get("content"),
            "reply_to_fact_id": action.get("reply_to_fact_id"),
        }))

    def execute_action(self, ctx: DshAuthContext, grant_id: str, action: dict,
                       *, requested_activity_id: str | None = None) -> dict:
        action_type = action.get("type")
        request_id = str(action.get("request_id") or "")
        if action_type not in ALLOWED_ACTIONS:
            exc = DshError(403, "DSH_ACTION_NOT_ALLOWED", "action not allowed")
            self._deny_action(ctx, grant_id, request_id, "", exc)
            raise exc
        if action_type == ACTION_SUBMIT_TURN:
            raw_content = action.get("content")
            if not isinstance(raw_content, str) or len(raw_content) > 4000:
                raise DshError(422, "DSH_SCHEMA_INVALID", "content must be at most 4000 characters")
            if not raw_content.strip():
                raise DshError(422, "DSH_SCHEMA_INVALID", "content must not be blank")
            action = {**action, "content": raw_content.strip()}
        request_hash = self.action_request_hash(grant_id, action)
        try:
            with self.store.transaction(immediate=True):
                # Admission time is taken only after the write lock is held.
                now = self._now()
                grant, subject = self._authorize(
                    ctx, grant_id, scope="ACTIVITY_PARTICIPATE", now=now)
                self._check_activity(grant, requested_activity_id)
                admitted_at = now
                if action_type not in grant["actions"]:
                    raise DshError(403, "DSH_ACTION_NOT_ALLOWED",
                                   "action not granted")
                receipt = self._receipt(grant_id, request_id)
                if receipt is not None:
                    if receipt["request_hash"] != request_hash:
                        raise DshError(409, "DSH_REQUEST_ID_CONFLICT", "request_id reused")
                    self.dsh.append_audit(
                        event_type="ACTION_REPLAY", outcome="REPLAY",
                        subject_id=ctx.subject_id, grant_id=grant_id,
                        request_id=request_id, resource_type="GRANT",
                        resource_id=grant_id, request_hash=request_hash,
                        detail={"type": action_type})
                    return {**receipt["outcome"], "replayed": True}
                if grant["actions_used"] >= grant["max_actions"]:
                    raise DshError(403, "DSH_QUOTA_EXHAUSTED", "action quota exhausted")
                participant = self._participant_row(grant["activity_id"], ctx.subject_id)
                if participant is None:
                    raise DshError(422, "DSH_PARTICIPANT_MISSING",
                                   "subject is not a participant")
                activity = self._activity(grant["activity_id"])
                if action_type == ACTION_LEAVE:
                    if activity["status"] == "ENDED":
                        raise DshError(409, "ACTIVITY_STATE_CONFLICT",
                                       "cannot leave an ended activity")
                    self.activities.set_participation_status(
                        grant["activity_id"],
                        dsh_participant_id(ctx.subject_id), "LEFT")
                    outcome = {
                        "grant_id": grant_id,
                        "activity_id": grant["activity_id"],
                        "type": ACTION_LEAVE,
                        "status": "LEFT",
                        "admitted_at": iso_utc(admitted_at),
                        "expires_at": grant["expires_at"],
                        "replayed": False,
                    }
                    self._commit_action(grant, action_type, request_id, request_hash,
                                        outcome, admitted_at, fact=None)
                    return outcome
                if str(participant["participation_status"]) != "ACTIVE":
                    raise DshError(409, "DSH_PARTICIPANT_LEFT", "subject has left")
                if activity["status"] != "ACTIVE":
                    raise DshError(409, "ACTIVITY_STATE_CONFLICT",
                                   "activity is not active")
                reply_to = action.get("reply_to_fact_id")
                if reply_to is not None:
                    if type(reply_to) is not int or reply_to < 1:
                        raise DshError(422, "DSH_SCHEMA_INVALID",
                                       "reply_to_fact_id must be a fact id")
                    try:
                        target = self.facts.get(grant["activity_id"], reply_to)
                    except ActivityFactCorrupt as exc:
                        raise DshError(503, "DSH_SCOPE_BROKEN",
                                       "stored fact scope is invalid") from exc
                    if target is None:
                        raise DshError(422, "DSH_SCHEMA_INVALID",
                                       "reply target is not in this activity")
                # Reserve the action inside the same transaction as the fact.
                self.store.conn.execute(
                    "UPDATE dsh_grants SET actions_used=actions_used+1 WHERE grant_id=?",
                    (grant_id,))
                self.dsh.append_audit(
                    event_type="ACTION_RESERVE", outcome="OK",
                    subject_id=ctx.subject_id, grant_id=grant_id,
                    request_id=request_id, resource_type="GRANT",
                    resource_id=grant_id, request_hash=request_hash,
                    detail={"type": action_type, "actions_used": grant["actions_used"] + 1},
                    at=admitted_at)
                if self._write_guard is not None:
                    self._write_guard()
                authorization = {
                    "grant_id": grant_id,
                    "subject_id": ctx.subject_id,
                    "status_at_commit": "ACTIVE",
                }
                try:
                    fact = self.facts.insert(
                        grant["activity_id"],
                        actor_type="DSH",
                        actor_key=dsh_participant_id(ctx.subject_id),
                        content=action["content"],
                        reply_to_fact_id=reply_to,
                        experience_scope=activity_experience_scope(grant["activity_id"]),
                        authorization=authorization,
                        now=admitted_at,
                    )
                except (ActivityFactConflict, ActivityFactCorrupt) as exc:
                    raise DshError(503, "DSH_SCOPE_BROKEN",
                                   "fact could not be written") from exc
                outcome = {
                    "grant_id": grant_id,
                    "activity_id": grant["activity_id"],
                    "type": ACTION_SUBMIT_TURN,
                    "fact": {
                        "fact_id": fact["fact_id"],
                        "seq": fact["seq"],
                        "actor": {
                            "type": "DSH",
                            "subject_id": ctx.subject_id,
                            "display_name": self._display_name(ctx.subject_id),
                        },
                        "content": fact["content"],
                        "reply_to_fact_id": fact["reply_to_fact_id"],
                        "experience_scope": fact["experience_scope"],
                        "authorization": fact["authorization"],
                        "created_at": fact["created_at"],
                    },
                    "admitted_at": iso_utc(admitted_at),
                    "expires_at": grant["expires_at"],
                    "replayed": False,
                }
                self._commit_action(grant, action_type, request_id, request_hash,
                                    outcome, admitted_at, fact=fact)
                return outcome
        except DshError as exc:
            if exc.status < 500:
                self._deny_action(ctx, grant_id, request_id, request_hash, exc)
            raise
        except Exception as exc:  # write failure: the transaction already rolled back
            try:
                self.dsh.audit_in_new_transaction(
                    event_type="ACTION_FAIL", outcome="ERROR",
                    subject_id=ctx.subject_id, grant_id=grant_id,
                    request_id=request_id, resource_type="GRANT",
                    resource_id=grant_id, request_hash=request_hash,
                    detail={"reason": type(exc).__name__})
            except DshError:
                raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed") from exc
            raise DshError(500, "DSH_WRITE_FAILED", "action write failed") from exc

    def _commit_action(self, grant: dict, action_type: str, request_id: str,
                       request_hash: str, outcome: dict, admitted_at: datetime,
                       *, fact: dict | None) -> None:
        self.dsh.put_receipt(
            self.store.conn, grant_id=grant["grant_id"], request_id=request_id,
            request_hash=request_hash,
            operation=action_type,
            outcome=outcome,
            consumed_actions=1,
            at=iso_utc(admitted_at))
        self.dsh.append_audit(
            event_type="ACTION_COMMIT", outcome="OK", subject_id=grant["subject_id"],
            grant_id=grant["grant_id"], request_id=request_id,
            resource_type="ACTIVITY", resource_id=grant["activity_id"],
            request_hash=request_hash,
            detail={"type": action_type, "admitted_at": iso_utc(admitted_at),
                    "expires_at": grant["expires_at"],
                    "fact_id": fact["fact_id"] if fact else None,
                    "seq": fact["seq"] if fact else None},
            at=admitted_at)

    # ------------------------------------------------------------ admin read

    def admin_facts(self, grant_id: str, *, after_seq: int = 0, limit: int = 50) -> dict:
        if type(after_seq) is not int or after_seq < 0:
            raise DshError(422, "DSH_SCHEMA_INVALID", "after_seq must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DshError(422, "DSH_SCHEMA_INVALID", "limit must be 1-50")
        with self.store.transaction():
            grant = self.dsh.get_grant(grant_id)
            if grant is None:
                raise DshError(403, "DSH_GRANT_MISSING", "grant not found")
            subject = self.dsh.get_subject(grant["subject_id"])
            revoked = (grant["status"] == "REVOKED"
                       or (subject is not None and subject["status"] == "REVOKED"))
            facts = self.facts.list(
                grant["activity_id"], after_seq=after_seq, limit=limit,
                grant_id=grant_id)
            projected = []
            for fact in facts:
                projected.append({**fact,
                                  "authorization_status":
                                      "REVOKED_AFTER_COMMIT" if revoked else "ACTIVE"})
            return {"grant_id": grant_id, "activity_id": grant["activity_id"],
                    "facts": projected}
