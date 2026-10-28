"""One SQLite owner for Activity state, participants and mutation receipts.

State, participants and the idempotency receipt commit together in one immediate
transaction, so a crash can never leave a status change without its receipt (or
vice versa). Reads never initialize a model, a runtime bundle or an embedding
provider; they only use the already-open store.

Activity-local identities are stored in the same participant table but their
``participant_key`` always carries the ``sim:`` prefix from
``domain.activity``. Nothing here consults the Persona registry, which is what
keeps a simulated participant from ever becoming a second Person.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from character_memory.domain.activity import (
    ActivityAction,
    ActivityCreate,
    MAX_ACTIVITY_VERSION,
    participant_display_name,
    participant_id,
    transition_target,
)


class ActivityMissing(LookupError):
    """No activity with that id."""


class ActivityConflict(ValueError):
    """Stale version, illegal transition, exhausted version or request reuse."""


class ActivityCorrupt(RuntimeError):
    """Stored activity data no longer matches the frozen contract."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivityRepository:
    def __init__(self, store, *, initialize: bool = True):
        self.store = store
        if initialize:
            store.apply_schema_migration("activity/001-lifecycle", self._migrate, immediate=True)
            # Separate ledger entry: 001 already ran on databases created by the
            # first revision of this slice, so folding the column into 001 would
            # leave those databases without it.
            store.apply_schema_migration("activity/002-updated-seq", self._migrate_ordering,
                                         immediate=True)
            # P13 adds the external DSH actor to the closed participant set.
            # A CHECK constraint cannot be widened in place, so this ledger
            # entry rebuilds the table once and copies every existing row.
            store.apply_schema_migration("activity/003-dsh-participants",
                                         self._migrate_dsh_participants, immediate=True)

    def _migrate(self) -> None:
        conn = self.store.conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS activities("
            "id TEXT PRIMARY KEY,"
            "title TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('CREATED','ACTIVE','PAUSED','ENDED')),"
            "version INTEGER NOT NULL CHECK(version>=1),"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,"
            "updated_seq INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS activity_participants("
            "activity_id TEXT NOT NULL REFERENCES activities(id),"
            "position INTEGER NOT NULL,"
            "participant_key TEXT NOT NULL,"
            "actor_type TEXT NOT NULL CHECK(actor_type IN ('CHARACTER','SIMULATED_CHARACTER')),"
            "character_id TEXT,"
            "local_id TEXT,"
            "display_name TEXT NOT NULL,"
            "PRIMARY KEY(activity_id,participant_key),"
            "UNIQUE(activity_id,position)"
            ")"
        )
        # One global request ledger: a create retry and an action retry share the
        # same idempotency namespace, so a retried request can never be applied
        # to a different activity.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS activity_receipts("
            "request_id TEXT PRIMARY KEY,"
            "request_hash TEXT NOT NULL,"
            "activity_id TEXT NOT NULL,"
            "operation TEXT NOT NULL,"
            "version_applied INTEGER NOT NULL,"
            "created_at TEXT NOT NULL"
            ")"
        )

    def _migrate_dsh_participants(self) -> None:
        """Widen ``activity_participants`` for external DSH subjects.

        Existing ``CHARACTER``/``SIMULATED_CHARACTER`` rows keep their
        ``participant_key`` and position; a DSH row stores ``subject_id`` and a
        ``participation_status``. The latter is ``ACTIVE`` for every pre-DSH row
        because the old contract had no way to leave an activity.
        """
        conn = self.store.conn
        if not self._exists():
            return
        columns = {str(row["name"])
                   for row in conn.execute("PRAGMA table_info(activity_participants)").fetchall()}
        if {"subject_id", "participation_status"} <= columns:
            return
        conn.execute("ALTER TABLE activity_participants RENAME TO activity_participants_pre_dsh")
        conn.execute(
            "CREATE TABLE activity_participants("
            "activity_id TEXT NOT NULL REFERENCES activities(id),"
            "position INTEGER NOT NULL,"
            "participant_key TEXT NOT NULL,"
            "actor_type TEXT NOT NULL CHECK(actor_type IN "
            "('CHARACTER','SIMULATED_CHARACTER','DSH')),"
            "character_id TEXT,"
            "local_id TEXT,"
            "subject_id TEXT,"
            "display_name TEXT NOT NULL,"
            "participation_status TEXT NOT NULL DEFAULT 'ACTIVE' "
            "CHECK(participation_status IN ('ACTIVE','LEFT')),"
            "PRIMARY KEY(activity_id,participant_key),"
            "UNIQUE(activity_id,position)"
            ")"
        )
        conn.execute(
            "INSERT INTO activity_participants("
            "activity_id,position,participant_key,actor_type,character_id,local_id,"
            "subject_id,display_name,participation_status) "
            "SELECT activity_id,position,participant_key,actor_type,character_id,local_id,"
            "NULL,display_name,'ACTIVE' FROM activity_participants_pre_dsh"
        )
        conn.execute("DROP TABLE activity_participants_pre_dsh")

    def _migrate_ordering(self) -> None:
        """Add the monotonic order key and backfill existing rows once.

        Wall-clock stamps are not an ordering key: Windows' clock granularity can
        return the same microsecond for two separate creations, which made
        "most recently updated" fall back to a random uuid tie-break. A
        store-assigned counter is monotonic within the durable database and
        survives restarts. Older databases get the column here, in its own
        ledger entry, because revision 001 has already been recorded for them.
        """
        conn = self.store.conn
        if not self._exists():
            return
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(activities)").fetchall()}
        if "updated_seq" not in columns:
            conn.execute("ALTER TABLE activities ADD COLUMN updated_seq INTEGER NOT NULL DEFAULT 0")
            # Insertion rowid preserves the original creation order, so a
            # backfilled database does not appear to have been rewritten.
            conn.execute("UPDATE activities SET updated_seq=rowid")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activities_updated ON activities(updated_seq DESC,id)"
        )

    @contextmanager
    def _snapshot(self):
        """Read on one snapshot; never commit or roll back a caller's transaction."""
        with self.store._lock:
            owned = not self.store.conn.in_transaction
            if owned:
                self.store.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                if owned:
                    self.store.conn.rollback()
                raise
            else:
                if owned:
                    self.store.conn.commit()

    def _exists(self) -> bool:
        return self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activities'"
        ).fetchone() is not None

    def _next_seq(self) -> int:
        """Monotonic store-assigned ordering key; caller holds the write lock.

        MAX+1 is safe under the immediate transaction every mutation opens: the
        second writer waits on SQLite's write lock, so two rows cannot receive
        the same value.
        """
        row = self.store.conn.execute("SELECT COALESCE(MAX(updated_seq),0)+1 FROM activities").fetchone()
        return int(row[0])

    def _participants(self, activity_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT * FROM activity_participants WHERE activity_id=? ORDER BY position",
            (activity_id,),
        ).fetchall()
        participants = []
        for row in rows:
            actor_type = str(row["actor_type"])
            if actor_type == "CHARACTER":
                participants.append({"actor_type": actor_type, "character_id": row["character_id"],
                                     "display_name": row["display_name"]})
            elif actor_type == "SIMULATED_CHARACTER":
                participants.append({"actor_type": actor_type, "local_id": row["local_id"],
                                     "display_name": row["display_name"]})
            elif actor_type == "DSH":
                participants.append({
                    "actor_type": actor_type,
                    "subject_id": str(row["subject_id"]),
                    "display_name": row["display_name"],
                    "participation_status": str(row["participation_status"]),
                })
            else:  # pragma: no cover - the CHECK constraint already refuses this
                raise ActivityCorrupt("unknown stored participant type")
        return participants

    def _row(self, activity_id: str) -> dict:
        row = self.store.conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if row is None:
            raise ActivityMissing("ACTIVITY_NOT_FOUND")
        status = str(row["status"])
        if status not in {"CREATED", "ACTIVE", "PAUSED", "ENDED"}:
            raise ActivityCorrupt("unknown stored activity status")
        if type(row["version"]) is not int or not 1 <= row["version"] <= MAX_ACTIVITY_VERSION:
            raise ActivityCorrupt("invalid stored activity version")
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "status": status,
            "version": row["version"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "participants": self._participants(activity_id),
        }

    def get(self, activity_id: str) -> dict:
        with self._snapshot():
            if not self._exists():
                raise ActivityMissing("ACTIVITY_NOT_FOUND")
            return self._row(activity_id)

    def get_participant(self, activity_id: str, participant_key: str) -> dict | None:
        """Read one participant row without leaving the caller's transaction."""
        if not self._exists():
            return None
        row = self.store.conn.execute(
            "SELECT * FROM activity_participants WHERE activity_id=? AND participant_key=?",
            (activity_id, participant_key)).fetchone()
        if row is None:
            return None
        return {
            "activity_id": str(row["activity_id"]),
            "participant_key": str(row["participant_key"]),
            "actor_type": str(row["actor_type"]),
            "subject_id": row["subject_id"],
            "display_name": str(row["display_name"]),
            "participation_status": str(row["participation_status"]),
        }

    def set_participation_status(self, activity_id: str, participant_key: str,
                                 status: str) -> None:
        """Set ACTIVE/LEFT for one participant. Caller owns the transaction."""
        if status not in {"ACTIVE", "LEFT"}:
            raise ValueError("invalid participation status")
        changed = self.store.conn.execute(
            "UPDATE activity_participants SET participation_status=? "
            "WHERE activity_id=? AND participant_key=?",
            (status, activity_id, participant_key))
        if changed.rowcount != 1:
            raise ActivityMissing("ACTIVITY_PARTICIPANT_NOT_FOUND")

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid activity limit")
        if status is not None and status not in {"CREATED", "ACTIVE", "PAUSED", "ENDED"}:
            raise ValueError("invalid activity status filter")
        with self._snapshot():
            if not self._exists():
                return []
            if status is None:
                rows = self.store.conn.execute(
                    "SELECT id FROM activities ORDER BY updated_seq DESC,id LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self.store.conn.execute(
                    "SELECT id FROM activities WHERE status=? ORDER BY updated_seq DESC,id LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [self._row(str(row["id"])) for row in rows]

    def _replay(self, request_id: str, request_hash: str) -> dict | None:
        row = self.store.conn.execute(
            "SELECT * FROM activity_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ActivityConflict("REQUEST_ID_CONFLICT")
        return self._response(str(row["activity_id"]), row, replayed=True)

    def _response(self, activity_id: str, receipt_row, *, replayed: bool) -> dict:
        return {
            "activity": self._row(activity_id),
            "receipt": {
                "request_id": str(receipt_row["request_id"]),
                "operation": str(receipt_row["operation"]),
                "version_applied": int(receipt_row["version_applied"]),
            },
            "replayed": replayed,
        }

    def _record(self, request_id: str, request_hash: str, activity_id: str,
                operation: str, version: int) -> dict:
        self.store.conn.execute(
            "INSERT INTO activity_receipts VALUES(?,?,?,?,?,?)",
            (request_id, request_hash, activity_id, operation, version, stamp()),
        )
        row = self.store.conn.execute(
            "SELECT * FROM activity_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._response(activity_id, row, replayed=False)

    def create(self, request: ActivityCreate, *,
               dsh_display_names: dict[str, str] | None = None) -> dict:
        request = ActivityCreate.model_validate(request.model_dump(mode="json"))
        request_hash = digest({"operation": "CREATE", "payload": request.model_dump(mode="json")})
        dsh_names = dsh_display_names or {}
        with self.store.transaction(immediate=True):
            replay = self._replay(request.request_id, request_hash)
            if replay is not None:
                return replay
            for participant in request.participants:
                if participant.actor_type == "DSH" and participant.subject_id not in dsh_names:
                    # The route resolves this before calling; the repository
                    # still refuses rather than inventing a display identity.
                    raise ActivityConflict("DSH_PARTICIPANT_MISSING")
            activity_id = uuid4().hex
            now = stamp()
            self.store.conn.execute(
                "INSERT INTO activities VALUES(?,?,?,?,?,?,?)",
                (activity_id, request.title, "CREATED", 1, now, now, self._next_seq()),
            )
            for position, participant in enumerate(request.participants):
                if participant.actor_type == "CHARACTER":
                    character_id = participant.character_id
                else:
                    character_id = None
                if participant.actor_type == "SIMULATED_CHARACTER":
                    local_id = participant.local_id
                else:
                    local_id = None
                if participant.actor_type == "DSH":
                    subject_id = participant.subject_id
                    display_name = dsh_names[participant.subject_id]
                else:
                    subject_id = None
                    display_name = participant_display_name(participant)
                self.store.conn.execute(
                    "INSERT INTO activity_participants("
                    "activity_id,position,participant_key,actor_type,character_id,"
                    "local_id,subject_id,display_name,participation_status) "
                    "VALUES(?,?,?,?,?,?,?,?, 'ACTIVE')",
                    (
                        activity_id,
                        position,
                        participant_id(participant),
                        participant.actor_type,
                        character_id,
                        local_id,
                        subject_id,
                        display_name,
                    ),
                )
            return self._record(request.request_id, request_hash, activity_id, "CREATE", 1)

    def act(self, activity_id: str, operation: str, request: ActivityAction) -> dict:
        request = ActivityAction.model_validate(request.model_dump(mode="json"))
        request_hash = digest({"activity_id": activity_id, "operation": operation,
                               "payload": request.model_dump(mode="json")})
        with self.store.transaction(immediate=True):
            replay = self._replay(request.request_id, request_hash)
            if replay is not None:
                return replay
            activity = self._row(activity_id)
            if activity["version"] != request.expected_version:
                raise ActivityConflict("ACTIVITY_VERSION_CONFLICT")
            if activity["version"] >= MAX_ACTIVITY_VERSION:
                raise ActivityConflict("ACTIVITY_VERSION_EXHAUSTED")
            try:
                target = transition_target(activity["status"], operation)
            except ValueError as exc:
                raise ActivityConflict("ACTIVITY_STATE_CONFLICT") from exc
            changed = self.store.conn.execute(
                "UPDATE activities SET status=?,version=version+1,updated_at=?,updated_seq=? "
                "WHERE id=? AND version=? AND status=?",
                (target, stamp(), self._next_seq(), activity_id,
                 request.expected_version, activity["status"]),
            )
            if changed.rowcount != 1:
                raise ActivityConflict("ACTIVITY_VERSION_CONFLICT")
            return self._record(request.request_id, request_hash, activity_id,
                                operation, request.expected_version + 1)
