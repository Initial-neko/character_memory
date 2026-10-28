"""Durable owner for relationship-practice scenarios and their simulated transcript.

A scenario reuses the Activity row for identity/status/version and adds the
scene description, partner notes, the transcript and one idempotent round per
user turn. Every event stores the frozen SIMULATED scope next to its content, so
the boundary travels with the row instead of being inferred later.

Nothing here writes ``events``, ``conversation_events``, ``memories`` or a
Persona. That is structural: the practice facts have their own table and no
product read path joins them to the real Person.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from character_memory.domain.activity import activity_experience_scope, participant_id
from character_memory.domain.relationship_scenario import (
    ScenarioAdoptRequest,
    ScenarioBranchCreate,
    ScenarioBranchOutput,
    ScenarioBranchRetry,
    ScenarioCreate,
    ScenarioReviewOutput,
    ScenarioReviewRequest,
    ScenarioRoundOutput,
    ScenarioRoundRequest,
    validate_round_output,
)
from character_memory.storage.activities import ActivityMissing


EVENT_TYPES = ("USER_MESSAGE", "PERSPECTIVE", "REPLY")


class ScenarioMissing(LookupError):
    """No scenario with that activity id."""


class ScenarioConflict(ValueError):
    """State, cast or idempotency conflict; the message is the stable code."""


class ScenarioCorrupt(RuntimeError):
    """Stored scenario data no longer matches the frozen contract."""


class ScenarioFailed(RuntimeError):
    """The round failed; the stored failure is the durable answer."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ScenarioRepository:
    def __init__(self, store, *, initialize: bool = True):
        self.store = store
        if initialize:
            store.apply_schema_migration("scenario/001-practice", self._migrate, immediate=True)
            # P12 extends the same store, not a second scenario runtime. The
            # migration runs in its own ledger entry so P11 databases gain the
            # branch column and review/adoption tables without re-running 001.
            store.apply_schema_migration("scenario/002-branch-review",
                                         self._migrate_branch_review, immediate=True)

    def _migrate(self) -> None:
        conn = self.store.conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS relationship_scenarios("
            "activity_id TEXT PRIMARY KEY,"
            "situation TEXT NOT NULL,"
            "goal TEXT NOT NULL,"
            "concern TEXT,"
            "created_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_participant_notes("
            "activity_id TEXT NOT NULL,"
            "participant_key TEXT NOT NULL,"
            "description TEXT NOT NULL,"
            "PRIMARY KEY(activity_id,participant_key)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "activity_id TEXT NOT NULL,"
            "turn_id TEXT NOT NULL,"
            "actor_type TEXT NOT NULL CHECK(actor_type IN ('USER','CHARACTER','SIMULATED_CHARACTER')),"
            "participant_key TEXT NOT NULL,"
            "event_type TEXT NOT NULL CHECK(event_type IN ('USER_MESSAGE','PERSPECTIVE','REPLY')),"
            "content TEXT NOT NULL,"
            "stance TEXT,"
            "created_at TEXT NOT NULL,"
            "experience_scope_json TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scenario_events_order "
            "ON scenario_events(activity_id,id)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_rounds("
            "activity_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "turn_id TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED')),"
            "failure_code TEXT,"
            "failure_detail TEXT,"
            "created_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "PRIMARY KEY(activity_id,request_id)"
            ")"
        )

    def _migrate_branch_review(self) -> None:
        conn = self.store.conn
        columns = {str(row["name"]) for row in conn.execute(
            "PRAGMA table_info(scenario_events)").fetchall()}
        if "branch_id" not in columns:
            # NULL is the main transcript. A branch id is server-generated and
            # travels with every branch event, so one branch can never be read
            # as another or as the main line.
            conn.execute("ALTER TABLE scenario_events ADD COLUMN branch_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scenario_events_branch_order "
            "ON scenario_events(activity_id,branch_id,id)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_branches("
            "activity_id TEXT NOT NULL,"
            "branch_id TEXT NOT NULL,"
            "after_event_id INTEGER NOT NULL,"
            "turn_id TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED')),"
            "consequence TEXT,"
            "failure_code TEXT,"
            "created_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "experience_scope_json TEXT NOT NULL,"
            "PRIMARY KEY(activity_id,branch_id),"
            "UNIQUE(activity_id,turn_id)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_branch_attempts("
            "activity_id TEXT NOT NULL,"
            "branch_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED')),"
            "failure_code TEXT,"
            "failure_detail TEXT,"
            "created_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "PRIMARY KEY(activity_id,request_id)"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scenario_branch_attempts_branch "
            "ON scenario_branch_attempts(activity_id,branch_id)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_reviews("
            "activity_id TEXT PRIMARY KEY,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED')),"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "overview TEXT,"
            "strategy TEXT,"
            "feedback TEXT,"
            "next_step TEXT,"
            "failure_code TEXT,"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "experience_scope_json TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_review_attempts("
            "activity_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED')),"
            "failure_code TEXT,"
            "failure_detail TEXT,"
            "created_at TEXT NOT NULL,"
            "finished_at TEXT,"
            "PRIMARY KEY(activity_id,request_id)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scenario_adoptions("
            "activity_id TEXT PRIMARY KEY,"
            "branch_id TEXT NOT NULL,"
            "character_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,"
            "event_id INTEGER NOT NULL,"
            "memory_id INTEGER NOT NULL,"
            "created_at TEXT NOT NULL,"
            "source_scope_json TEXT NOT NULL,"
            "result_scope_json TEXT NOT NULL"
            ")"
        )

    # -- reads -----------------------------------------------------------

    @staticmethod
    def _event(row) -> dict:
        scope = json.loads(row["experience_scope_json"])
        branch_id = row["branch_id"]
        expected = activity_experience_scope(
            str(row["activity_id"]), branch_id=str(branch_id) if branch_id is not None else None)
        # The scope is not decoration: a row whose stored scope no longer names
        # its own activity/branch is corrupt, not "real enough to show".
        if not isinstance(scope, dict) or scope != expected:
            raise ScenarioCorrupt("stored scenario event lost its simulated scope")
        return {
            "id": int(row["id"]),
            "turn_id": str(row["turn_id"]),
            "branch_id": str(branch_id) if branch_id is not None else None,
            "actor_type": str(row["actor_type"]),
            "participant_key": str(row["participant_key"]),
            "event_type": str(row["event_type"]),
            "content": str(row["content"]),
            "stance": row["stance"],
            "created_at": str(row["created_at"]),
            "experience_scope": scope,
        }

    def _scenario_row(self, activity_id: str):
        row = self.store.conn.execute(
            "SELECT * FROM relationship_scenarios WHERE activity_id=?", (activity_id,)
        ).fetchone()
        if row is None:
            raise ScenarioMissing("SCENARIO_NOT_FOUND")
        return row

    def _notes(self, activity_id: str) -> dict[str, str]:
        rows = self.store.conn.execute(
            "SELECT participant_key,description FROM scenario_participant_notes WHERE activity_id=?",
            (activity_id,),
        ).fetchall()
        return {str(row["participant_key"]): str(row["description"]) for row in rows}

    def events(self, activity_id: str, *, limit: int | None = None,
               branch_id: str | None = None) -> list[dict]:
        """Read one transcript only.

        ``branch_id=None`` is the main line. A branch read supplies its own id,
        so the two never share a result set and cannot contaminate each other.
        """
        branch_sql = "branch_id IS NULL" if branch_id is None else "branch_id=?"
        branch_args: list = [] if branch_id is None else [branch_id]
        with self.store._lock:
            sql = ("SELECT * FROM scenario_events WHERE activity_id=? AND "
                   + branch_sql + " ORDER BY id")
            args: list = [activity_id, *branch_args]
            if limit is not None:
                sql = ("SELECT * FROM (SELECT * FROM scenario_events WHERE activity_id=? AND "
                       + branch_sql + " ORDER BY id DESC LIMIT ?) ORDER BY id")
                args = [activity_id, *branch_args, int(limit)]
            return [self._event(row) for row in self.store.conn.execute(sql, args).fetchall()]

    def branch_events(self, activity_id: str, branch_id: str) -> list[dict]:
        return self.events(activity_id, branch_id=branch_id)

    def latest_event_id(self, activity_id: str) -> int | None:
        """Watermark of the main transcript; branches never move it."""
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT MAX(id) FROM scenario_events WHERE activity_id=? AND branch_id IS NULL",
                (activity_id,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None

    def rounds(self, activity_id: str) -> list[dict]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM scenario_rounds WHERE activity_id=? ORDER BY created_at,rowid",
                (activity_id,),
            ).fetchall()
            return [{"request_id": str(row["request_id"]), "turn_id": str(row["turn_id"]),
                     "status": str(row["status"]), "failure_code": row["failure_code"],
                     "created_at": str(row["created_at"]), "finished_at": row["finished_at"]}
                    for row in rows]

    def get(self, activity_id: str) -> dict:
        with self.store._lock:
            row = self._scenario_row(activity_id)
            notes = self._notes(activity_id)
            return {
                "activity_id": activity_id,
                "situation": str(row["situation"]),
                "goal": str(row["goal"]),
                "concern": row["concern"],
                "created_at": str(row["created_at"]),
                "notes": notes,
                "events": self.events(activity_id),
                "rounds": self.rounds(activity_id),
                "branches": self.branches(activity_id),
                "adoption": self.adoption(activity_id),
            }

    def exists(self, activity_id: str) -> bool:
        with self.store._lock:
            return self.store.conn.execute(
                "SELECT 1 FROM relationship_scenarios WHERE activity_id=?", (activity_id,)
            ).fetchone() is not None

    # -- writes ----------------------------------------------------------

    def create(self, request: ScenarioCreate, activity: dict) -> dict:
        """Persist the scene beside an already-created Activity row.

        The caller has already inserted the Activity in the same transaction, so
        a failure here rolls the whole scene back instead of leaving an orphan
        Activity that no scenario owns.
        """
        activity_id = str(activity["id"])
        keys = {participant_id(item) for item in request.participants}
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "INSERT INTO relationship_scenarios(activity_id,situation,goal,concern,created_at) "
                "VALUES(?,?,?,?,?)",
                (activity_id, request.situation, request.goal, request.concern, _stamp()),
            )
            for key, text in request.descriptions.items():
                if key not in keys:  # pragma: no cover - the model validator already refused it
                    raise ScenarioConflict("description targets an unknown participant")
                self.store.conn.execute(
                    "INSERT INTO scenario_participant_notes(activity_id,participant_key,description) "
                    "VALUES(?,?,?)",
                    (activity_id, key, text),
                )
            return self.get(activity_id)

    def _activity_status(self, activity_id: str) -> tuple[str, int]:
        row = self.store.conn.execute(
            "SELECT status,version FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        if row is None:
            raise ScenarioMissing("SCENARIO_NOT_FOUND")
        return str(row["status"]), int(row["version"])

    def _round(self, activity_id: str, request: ScenarioRoundRequest) -> dict | None:
        row = self.store.conn.execute(
            "SELECT * FROM scenario_rounds WHERE activity_id=? AND request_id=?",
            (activity_id, request.request_id),
        ).fetchone()
        if row is None:
            return None
        request_hash = _digest({"activity_id": activity_id,
                                "payload": request.model_dump(mode="json")})
        if row["request_hash"] != request_hash:
            raise ScenarioConflict("REQUEST_ID_CONFLICT")
        if row["status"] == "FAILED":
            raise ScenarioFailed(str(row["failure_code"] or "SCENARIO_GENERATION_FAILED"),
                                 str(row["failure_detail"] or ""))
        if row["status"] == "PENDING":
            # A previous call never reported an outcome. We do not silently retry
            # it under the same id: the user's own turn stays visible and the
            # missing partner facts are reported as unresolved.
            raise ScenarioConflict("SCENARIO_ROUND_PENDING")
        return row

    def _turn_events(self, activity_id: str, turn_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT * FROM scenario_events WHERE activity_id=? AND turn_id=? "
            "AND branch_id IS NULL ORDER BY id",
            (activity_id, turn_id),
        ).fetchall()
        return [self._event(row) for row in rows]

    def _present_round(self, activity_id: str, turn_id: str, *, replayed: bool) -> dict:
        events = self._turn_events(activity_id, turn_id)
        return {
            "replayed": replayed,
            "turn_id": turn_id,
            "events": events,
            "perspectives": [item for item in events if item["event_type"] == "PERSPECTIVE"],
            "replies": [item for item in events if item["event_type"] == "REPLY"],
            "experience_scope": activity_experience_scope(activity_id),
        }

    def begin_round(self, activity_id: str, request: ScenarioRoundRequest, *,
                    on_user_event=None) -> dict:
        """Record the user's turn and claim the round; replay returns the same result.

        Returns ``{"claimed": False, ...replay...}`` when this request already
        has a durable outcome. Otherwise returns the new turn id and the user
        event id, and the caller must call the model. ``on_user_event`` runs in
        the same transaction as the user event (used to bind draft attribution),
        so an attribution conflict cannot leave an accepted message behind.
        """
        with self.store.transaction(immediate=True):
            if not self.exists(activity_id):
                raise ScenarioMissing("SCENARIO_NOT_FOUND")
            previous = self._round(activity_id, request)
            if previous is not None:
                return {"claimed": False,
                        **self._present_round(activity_id, str(previous["turn_id"]), replayed=True)}
            status, _ = self._activity_status(activity_id)
            if status != "ACTIVE":
                # Pausing or ending a scene must stop new simulated consequences.
                raise ScenarioConflict("SCENARIO_NOT_ACTIVE")
            turn_id = uuid4().hex
            scope_json = _canonical(activity_experience_scope(activity_id))
            request_hash = _digest({"activity_id": activity_id,
                                    "payload": request.model_dump(mode="json")})
            self.store.conn.execute(
                "INSERT INTO scenario_rounds(activity_id,request_id,request_hash,turn_id,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (activity_id, request.request_id, request_hash, turn_id, "PENDING", _stamp()),
            )
            inserted = self.store.conn.execute(
                "INSERT INTO scenario_events(activity_id,turn_id,actor_type,participant_key,"
                "event_type,content,stance,created_at,experience_scope_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (activity_id, turn_id, "USER", "user", "USER_MESSAGE", request.message, None,
                 _stamp(), scope_json),
            )
            user_event_id = int(inserted.lastrowid)
            if on_user_event is not None:
                on_user_event(user_event_id)
            return {"claimed": True, "turn_id": turn_id, "user_event_id": user_event_id,
                    "experience_scope": activity_experience_scope(activity_id)}

    def fail_round(self, activity_id: str, request: ScenarioRoundRequest, code: str,
                   detail: str = "") -> None:
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "UPDATE scenario_rounds SET status='FAILED',failure_code=?,failure_detail=?,finished_at=? "
                "WHERE activity_id=? AND request_id=? AND status='PENDING'",
                (code, detail[:2000], _stamp(), activity_id, request.request_id),
            )

    def commit_round(self, activity_id: str, request: ScenarioRoundRequest, turn_id: str,
                     output: ScenarioRoundOutput, *, participants: list[dict]) -> dict:
        by_key = {str(item["participant_key"]): item for item in participants}
        simulated = {key for key, item in by_key.items() if item["actor_type"] == "SIMULATED_CHARACTER"}
        validate_round_output(output, participant_ids=list(by_key), simulated_ids=list(simulated))
        with self.store.transaction(immediate=True):
            status, _ = self._activity_status(activity_id)
            if status != "ACTIVE":
                raise ScenarioConflict("SCENARIO_STATE_CHANGED")
            scope_json = _canonical(activity_experience_scope(activity_id))
            now = _stamp()
            for perspective in output.perspectives:
                participant = by_key[perspective.participant_id]
                self.store.conn.execute(
                    "INSERT INTO scenario_events(activity_id,turn_id,actor_type,participant_key,"
                    "event_type,content,stance,created_at,experience_scope_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (activity_id, turn_id, participant["actor_type"], perspective.participant_id,
                     "PERSPECTIVE", perspective.perspective, perspective.stance, now, scope_json),
                )
            for reply in output.replies:
                participant = by_key[reply.participant_id]
                self.store.conn.execute(
                    "INSERT INTO scenario_events(activity_id,turn_id,actor_type,participant_key,"
                    "event_type,content,stance,created_at,experience_scope_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (activity_id, turn_id, participant["actor_type"], reply.participant_id,
                     "REPLY", reply.message, None, now, scope_json),
                )
            changed = self.store.conn.execute(
                "UPDATE scenario_rounds SET status='READY',finished_at=? "
                "WHERE activity_id=? AND request_id=? AND status='PENDING'",
                (now, activity_id, request.request_id),
            )
            if changed.rowcount != 1:
                raise ScenarioConflict("SCENARIO_ROUND_PENDING")
            return self._present_round(activity_id, turn_id, replayed=False)

    def abandoned_rounds(self, activity_id: str) -> list[dict]:
        """Rounds whose model call never reported an outcome."""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT turn_id,request_id FROM scenario_rounds "
                "WHERE activity_id=? AND status='PENDING'", (activity_id,),
            ).fetchall()
            return [{"turn_id": str(row["turn_id"]), "request_id": str(row["request_id"])}
                    for row in rows]

    def forget_pending(self, activity_id: str, request_id: str) -> bool:
        """Retire an unresolved claim so the operator can resend deliberately."""
        with self.store.transaction(immediate=True):
            changed = self.store.conn.execute(
                "UPDATE scenario_rounds SET status='FAILED',failure_code='SCENARIO_ROUND_UNRESOLVED',"
                "failure_detail='no partner facts were produced',finished_at=? "
                "WHERE activity_id=? AND request_id=? AND status='PENDING'",
                (_stamp(), activity_id, request_id),
            )
            return changed.rowcount == 1

    # -- suggestion snapshot --------------------------------------------

    def suggestion_snapshot(self, activity_id: str, event_id: int) -> dict:
        """Visible practice context for a draft, with the same staleness rule.

        Only scenario rows are read: a practice draft can never be generated
        from real direct/group facts, and the real generation path cannot see
        these rows because they live in a different table.
        """
        with self.store._lock:
            row = self._scenario_row(activity_id)
            notes = self._notes(activity_id)
            latest = self.latest_event_id(activity_id)
            if latest is None or latest != int(event_id):
                raise ScenarioConflict("SCENARIO_CONTEXT_CHANGED")
            rows = self.store.conn.execute(
                "SELECT * FROM scenario_events WHERE activity_id=? AND branch_id IS NULL "
                "ORDER BY id DESC LIMIT 12",
                (activity_id,),
            ).fetchall()
            events = [self._event(item) for item in reversed(rows)]
            if not events or events[-1]["id"] != int(event_id):
                raise ScenarioConflict("SCENARIO_CONTEXT_CHANGED")
            participants = []
            for item in self.store.conn.execute(
                    "SELECT participant_key,actor_type,character_id,local_id,display_name "
                    "FROM activity_participants WHERE activity_id=? ORDER BY position",
                    (activity_id,)).fetchall():
                participants.append({
                    "participant_key": str(item["participant_key"]),
                    "actor_type": str(item["actor_type"]),
                    "display_name": str(item["display_name"]),
                })
            return {
                "scenario": {
                    "activity_id": activity_id,
                    "situation": str(row["situation"]),
                    "goal": str(row["goal"]),
                    "concern": row["concern"],
                    "notes": notes,
                },
                "participants": participants,
                "events": events,
                "watermark": latest,
            }

    # -- branches --------------------------------------------------------

    def _branch_summary(self, row) -> dict:
        scope = json.loads(row["experience_scope_json"])
        branch_id = str(row["branch_id"])
        expected = activity_experience_scope(str(row["activity_id"]), branch_id=branch_id)
        if not isinstance(scope, dict) or scope != expected:
            raise ScenarioCorrupt("stored branch lost its simulated scope")
        return {
            "activity_id": str(row["activity_id"]),
            "branch_id": branch_id,
            "after_event_id": int(row["after_event_id"]),
            "turn_id": str(row["turn_id"]),
            "status": str(row["status"]),
            "consequence": row["consequence"],
            "failure_code": row["failure_code"],
            "created_at": str(row["created_at"]),
            "finished_at": row["finished_at"],
            "experience_scope": scope,
        }

    def _branch_row(self, activity_id: str, branch_id: str):
        row = self.store.conn.execute(
            "SELECT * FROM scenario_branches WHERE activity_id=? AND branch_id=?",
            (activity_id, branch_id),
        ).fetchone()
        if row is None:
            raise ScenarioMissing("SCENARIO_BRANCH_NOT_FOUND")
        return row

    def branches(self, activity_id: str) -> list[dict]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM scenario_branches WHERE activity_id=? ORDER BY created_at,rowid",
                (activity_id,),
            ).fetchall()
            return [self._branch_summary(row) for row in rows]

    def branch(self, activity_id: str, branch_id: str) -> dict:
        with self.store._lock:
            row = self._branch_row(activity_id, branch_id)
            return {**self._branch_summary(row),
                    "events": self.events(activity_id, branch_id=branch_id)}

    def _branch_attempt_row(self, activity_id: str, request_id: str):
        return self.store.conn.execute(
            "SELECT * FROM scenario_branch_attempts WHERE activity_id=? AND request_id=?",
            (activity_id, request_id),
        ).fetchone()

    @staticmethod
    def _branch_hash(activity_id: str, operation: str, payload: dict) -> str:
        return _digest({"activity_id": activity_id, "operation": operation,
                        "payload": payload})

    def _branch_replay(self, activity_id: str, request_id: str, request_hash: str,
                       *, expected_branch_id: str | None = None) -> dict | None:
        row = self._branch_attempt_row(activity_id, request_id)
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ScenarioConflict("REQUEST_ID_CONFLICT")
        branch_id = str(row["branch_id"])
        if expected_branch_id is not None and branch_id != expected_branch_id:
            raise ScenarioConflict("REQUEST_ID_CONFLICT")
        if row["status"] == "FAILED":
            raise ScenarioFailed(str(row["failure_code"] or "SCENARIO_BRANCH_GENERATION_FAILED"),
                                 str(row["failure_detail"] or ""))
        if row["status"] == "PENDING":
            # The same request id never starts a second model call.
            raise ScenarioConflict("SCENARIO_BRANCH_PENDING")
        return {"claimed": False, "replayed": True,
                **self.branch(activity_id, branch_id)}

    def begin_branch(self, activity_id: str, request: ScenarioBranchCreate) -> dict:
        """Claim one branch generation at an existing main-transcript event.

        The branch row, its alternative user turn and the attempt receipt commit
        together. Partner content is written only after the model result passes
        validation, so a failed branch never leaves fabricated views behind.
        """
        request_hash = self._branch_hash(
            activity_id, "CREATE_BRANCH",
            {"after_event_id": int(request.after_event_id), "message": request.message})
        with self.store.transaction(immediate=True):
            replay = self._branch_replay(activity_id, request.request_id, request_hash)
            if replay is not None:
                return replay
            if not self.exists(activity_id):
                raise ScenarioMissing("SCENARIO_NOT_FOUND")
            status, _ = self._activity_status(activity_id)
            if status != "ACTIVE":
                raise ScenarioConflict("SCENARIO_NOT_ACTIVE")
            point = self.store.conn.execute(
                "SELECT id FROM scenario_events WHERE activity_id=? AND branch_id IS NULL AND id=?",
                (activity_id, int(request.after_event_id)),
            ).fetchone()
            if point is None:
                raise ScenarioConflict("SCENARIO_BRANCH_POINT_INVALID")
            branch_id = uuid4().hex
            turn_id = uuid4().hex
            scope_json = _canonical(activity_experience_scope(activity_id, branch_id=branch_id))
            now = _stamp()
            self.store.conn.execute(
                "INSERT INTO scenario_branches(activity_id,branch_id,after_event_id,turn_id,status,"
                "created_at,experience_scope_json) VALUES(?,?,?,?,?,?,?)",
                (activity_id, branch_id, int(request.after_event_id), turn_id, "PENDING",
                 now, scope_json),
            )
            self.store.conn.execute(
                "INSERT INTO scenario_events(activity_id,branch_id,turn_id,actor_type,"
                "participant_key,event_type,content,stance,created_at,experience_scope_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (activity_id, branch_id, turn_id, "USER", "user", "USER_MESSAGE",
                 request.message, None, now, scope_json),
            )
            self.store.conn.execute(
                "INSERT INTO scenario_branch_attempts(activity_id,branch_id,request_id,"
                "request_hash,status,created_at) VALUES(?,?,?,?,?,?)",
                (activity_id, branch_id, request.request_id, request_hash, "PENDING", now),
            )
            return {"claimed": True, "branch_id": branch_id, "turn_id": turn_id,
                    "after_event_id": int(request.after_event_id),
                    "message": request.message,
                    "experience_scope": activity_experience_scope(activity_id, branch_id=branch_id)}

    def begin_branch_retry(self, activity_id: str, branch_id: str,
                           request: ScenarioBranchRetry) -> dict:
        """Start a fresh attempt for a durably FAILED branch.

        The alternative user turn is reused; only partner output is generated
        again. The previously failed request id keeps returning its own failure.
        """
        request_hash = self._branch_hash(activity_id, "RETRY_BRANCH", {})
        with self.store.transaction(immediate=True):
            replay = self._branch_replay(activity_id, request.request_id, request_hash,
                                         expected_branch_id=branch_id)
            if replay is not None:
                return replay
            row = self._branch_row(activity_id, branch_id)
            summary = self._branch_summary(row)
            if summary["status"] == "READY":
                return {"claimed": False, "replayed": True,
                        **self.branch(activity_id, branch_id)}
            if summary["status"] == "PENDING":
                raise ScenarioConflict("SCENARIO_BRANCH_PENDING")
            status, _ = self._activity_status(activity_id)
            if status != "ACTIVE":
                raise ScenarioConflict("SCENARIO_NOT_ACTIVE")
            user = self.store.conn.execute(
                "SELECT content FROM scenario_events WHERE activity_id=? AND branch_id=? "
                "AND event_type='USER_MESSAGE' ORDER BY id LIMIT 1",
                (activity_id, branch_id),
            ).fetchone()
            if user is None:
                raise ScenarioCorrupt("stored branch lost its alternative user turn")
            now = _stamp()
            self.store.conn.execute(
                "UPDATE scenario_branches SET status='PENDING',failure_code=NULL,finished_at=NULL "
                "WHERE activity_id=? AND branch_id=? AND status='FAILED'",
                (activity_id, branch_id),
            )
            self.store.conn.execute(
                "INSERT INTO scenario_branch_attempts(activity_id,branch_id,request_id,"
                "request_hash,status,created_at) VALUES(?,?,?,?,?,?)",
                (activity_id, branch_id, request.request_id, request_hash, "PENDING", now),
            )
            return {"claimed": True, "branch_id": branch_id,
                    "turn_id": summary["turn_id"],
                    "after_event_id": summary["after_event_id"],
                    "message": str(user["content"]),
                    "experience_scope": activity_experience_scope(
                        activity_id, branch_id=branch_id)}

    def fail_branch(self, activity_id: str, branch_id: str, request_id: str, code: str,
                    detail: str = "") -> None:
        with self.store.transaction(immediate=True):
            now = _stamp()
            self.store.conn.execute(
                "UPDATE scenario_branches SET status='FAILED',failure_code=?,finished_at=? "
                "WHERE activity_id=? AND branch_id=? AND status='PENDING'",
                (code, now, activity_id, branch_id),
            )
            self.store.conn.execute(
                "UPDATE scenario_branch_attempts SET status='FAILED',failure_code=?,"
                "failure_detail=?,finished_at=? WHERE activity_id=? AND branch_id=? "
                "AND request_id=? AND status='PENDING'",
                (code, detail[:2000], now, activity_id, branch_id, request_id),
            )

    def commit_branch(self, activity_id: str, branch_id: str, request_id: str, turn_id: str,
                      output: ScenarioBranchOutput, *, participants: list[dict]) -> dict:
        by_key = {str(item["participant_key"]): item for item in participants}
        simulated = {key for key, item in by_key.items()
                     if item["actor_type"] == "SIMULATED_CHARACTER"}
        validate_round_output(output, participant_ids=list(by_key),
                              simulated_ids=list(simulated))
        with self.store.transaction(immediate=True):
            summary = self._branch_summary(self._branch_row(activity_id, branch_id))
            if summary["status"] != "PENDING":
                raise ScenarioConflict("SCENARIO_BRANCH_PENDING")
            status, _ = self._activity_status(activity_id)
            if status != "ACTIVE":
                raise ScenarioConflict("SCENARIO_STATE_CHANGED")
            scope_json = _canonical(activity_experience_scope(
                activity_id, branch_id=branch_id))
            now = _stamp()
            for perspective in output.perspectives:
                participant = by_key[perspective.participant_id]
                self.store.conn.execute(
                    "INSERT INTO scenario_events(activity_id,branch_id,turn_id,actor_type,"
                    "participant_key,event_type,content,stance,created_at,experience_scope_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (activity_id, branch_id, turn_id, participant["actor_type"],
                     perspective.participant_id, "PERSPECTIVE", perspective.perspective,
                     perspective.stance, now, scope_json),
                )
            for reply in output.replies:
                participant = by_key[reply.participant_id]
                self.store.conn.execute(
                    "INSERT INTO scenario_events(activity_id,branch_id,turn_id,actor_type,"
                    "participant_key,event_type,content,stance,created_at,experience_scope_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (activity_id, branch_id, turn_id, participant["actor_type"],
                     reply.participant_id, "REPLY", reply.message, None, now, scope_json),
                )
            changed = self.store.conn.execute(
                "UPDATE scenario_branches SET status='READY',consequence=?,finished_at=? "
                "WHERE activity_id=? AND branch_id=? AND status='PENDING'",
                (output.consequence, now, activity_id, branch_id),
            )
            if changed.rowcount != 1:
                raise ScenarioConflict("SCENARIO_BRANCH_PENDING")
            self.store.conn.execute(
                "UPDATE scenario_branch_attempts SET status='READY',finished_at=? "
                "WHERE activity_id=? AND branch_id=? AND request_id=? AND status='PENDING'",
                (now, activity_id, branch_id, request_id),
            )
            return self.branch(activity_id, branch_id)

    # -- review ----------------------------------------------------------

    def _review_attempt_row(self, activity_id: str, request_id: str):
        return self.store.conn.execute(
            "SELECT * FROM scenario_review_attempts WHERE activity_id=? AND request_id=?",
            (activity_id, request_id),
        ).fetchone()

    @staticmethod
    def _review_hash(activity_id: str) -> str:
        return _digest({"activity_id": activity_id, "operation": "REVIEW_SCENARIO"})

    def _review_payload(self, row) -> dict:
        scope = json.loads(row["experience_scope_json"])
        expected = activity_experience_scope(str(row["activity_id"]))
        if not isinstance(scope, dict) or scope != expected:
            raise ScenarioCorrupt("stored review lost its simulated scope")
        return {
            "activity_id": str(row["activity_id"]),
            "status": str(row["status"]),
            "request_id": str(row["request_id"]),
            "overview": row["overview"],
            "strategy": row["strategy"],
            "feedback": row["feedback"],
            "next_step": row["next_step"],
            "failure_code": row["failure_code"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": row["finished_at"],
            "experience_scope": scope,
        }

    def review(self, activity_id: str) -> dict:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM scenario_reviews WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if row is None:
                return {"activity_id": activity_id, "status": "NONE", "review": None,
                        "experience_scope": activity_experience_scope(activity_id)}
            payload = self._review_payload(row)
            return {"activity_id": activity_id, "status": payload["status"],
                    "review": payload, "experience_scope": payload["experience_scope"]}

    def _review_replay(self, activity_id: str, request_id: str,
                       request_hash: str) -> dict | None:
        row = self._review_attempt_row(activity_id, request_id)
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ScenarioConflict("REQUEST_ID_CONFLICT")
        if row["status"] == "FAILED":
            raise ScenarioFailed(str(row["failure_code"] or "SCENARIO_REVIEW_FAILED"),
                                 str(row["failure_detail"] or ""))
        if row["status"] == "PENDING":
            raise ScenarioConflict("SCENARIO_REVIEW_PENDING")
        return {"claimed": False, "replayed": True, **self.review(activity_id)}

    def begin_review(self, activity_id: str, request: ScenarioReviewRequest) -> dict:
        request_hash = self._review_hash(activity_id)
        with self.store.transaction(immediate=True):
            replay = self._review_replay(activity_id, request.request_id, request_hash)
            if replay is not None:
                return replay
            if not self.exists(activity_id):
                raise ScenarioMissing("SCENARIO_NOT_FOUND")
            current = self.review(activity_id)
            if current["status"] == "READY":
                # One durable review per scenario. A new request id reads the
                # existing result instead of paying for another model call.
                return {"claimed": False, "replayed": True, **current}
            if current["status"] == "PENDING":
                raise ScenarioConflict("SCENARIO_REVIEW_PENDING")
            status, _ = self._activity_status(activity_id)
            if status != "ENDED":
                raise ScenarioConflict("SCENARIO_NOT_ENDED")
            scope_json = _canonical(activity_experience_scope(activity_id))
            now = _stamp()
            if current["status"] == "NONE":
                self.store.conn.execute(
                    "INSERT INTO scenario_reviews(activity_id,status,request_id,request_hash,"
                    "created_at,updated_at,experience_scope_json) VALUES(?,?,?,?,?,?,?)",
                    (activity_id, "PENDING", request.request_id, request_hash, now, now,
                     scope_json),
                )
            else:
                self.store.conn.execute(
                    "UPDATE scenario_reviews SET status='PENDING',request_id=?,request_hash=?,"
                    "overview=NULL,strategy=NULL,feedback=NULL,next_step=NULL,failure_code=NULL,"
                    "updated_at=?,finished_at=NULL WHERE activity_id=?",
                    (request.request_id, request_hash, now, activity_id),
                )
            self.store.conn.execute(
                "INSERT INTO scenario_review_attempts(activity_id,request_id,request_hash,"
                "status,created_at) VALUES(?,?,?,?,?)",
                (activity_id, request.request_id, request_hash, "PENDING", now),
            )
            return {"claimed": True, "experience_scope": activity_experience_scope(activity_id)}

    def fail_review(self, activity_id: str, request_id: str, code: str, detail: str = "") -> None:
        with self.store.transaction(immediate=True):
            now = _stamp()
            self.store.conn.execute(
                "UPDATE scenario_reviews SET status='FAILED',failure_code=?,updated_at=?,"
                "finished_at=? WHERE activity_id=? AND status='PENDING' AND request_id=?",
                (code, now, now, activity_id, request_id),
            )
            self.store.conn.execute(
                "UPDATE scenario_review_attempts SET status='FAILED',failure_code=?,"
                "failure_detail=?,finished_at=? WHERE activity_id=? AND request_id=? "
                "AND status='PENDING'",
                (code, detail[:2000], now, activity_id, request_id),
            )

    def commit_review(self, activity_id: str, request_id: str,
                      output: ScenarioReviewOutput) -> dict:
        with self.store.transaction(immediate=True):
            status, _ = self._activity_status(activity_id)
            if status != "ENDED":
                raise ScenarioConflict("SCENARIO_STATE_CHANGED")
            now = _stamp()
            changed = self.store.conn.execute(
                "UPDATE scenario_reviews SET status='READY',overview=?,strategy=?,feedback=?,"
                "next_step=?,failure_code=NULL,updated_at=?,finished_at=? "
                "WHERE activity_id=? AND status='PENDING' AND request_id=?",
                (output.overview, output.strategy, output.feedback, output.next_step,
                 now, now, activity_id, request_id),
            )
            if changed.rowcount != 1:
                raise ScenarioConflict("SCENARIO_REVIEW_PENDING")
            self.store.conn.execute(
                "UPDATE scenario_review_attempts SET status='READY',finished_at=? "
                "WHERE activity_id=? AND request_id=? AND status='PENDING'",
                (now, activity_id, request_id),
            )
            return self.review(activity_id)

    # -- explicit adoption ----------------------------------------------

    def adoption(self, activity_id: str) -> dict | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM scenario_adoptions WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if row is None:
                return None
            source = json.loads(row["source_scope_json"])
            result = json.loads(row["result_scope_json"])
            branch_id = str(row["branch_id"])
            expected_source = activity_experience_scope(activity_id, branch_id=branch_id)
            if (not isinstance(source, dict) or source != expected_source
                    or result != {"schema_version": 1, "reality": "REAL"}):
                raise ScenarioCorrupt("stored adoption scope is invalid")
            return {
                "activity_id": activity_id,
                "branch_id": branch_id,
                "character_id": str(row["character_id"]),
                "request_id": str(row["request_id"]),
                "event_id": int(row["event_id"]),
                "memory_id": int(row["memory_id"]),
                "created_at": str(row["created_at"]),
                "source_scope": source,
                "result_scope": result,
            }

    @staticmethod
    def adoption_hash(activity_id: str, request: ScenarioAdoptRequest) -> str:
        return _digest({"activity_id": activity_id, "operation": "ADOPT_BRANCH",
                        "payload": request.model_dump(mode="json")})

    def record_adoption(self, activity_id: str, request: ScenarioAdoptRequest, *,
                        character_id: str, event_id: int, memory_id: int) -> dict:
        """Persist the adoption receipt in the caller's open write transaction."""
        branch_id = request.branch_id
        request_hash = self.adoption_hash(activity_id, request)
        self.store.conn.execute(
            "INSERT INTO scenario_adoptions(activity_id,branch_id,character_id,request_id,"
            "request_hash,event_id,memory_id,created_at,source_scope_json,result_scope_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (activity_id, branch_id, character_id, request.request_id, request_hash,
             int(event_id), int(memory_id), _stamp(),
             _canonical(activity_experience_scope(activity_id, branch_id=branch_id)),
             _canonical({"schema_version": 1, "reality": "REAL"})),
        )
        return self.adoption(activity_id)

    def adoption_hash_for(self, activity_id: str, request: ScenarioAdoptRequest) -> str:
        return self.adoption_hash(activity_id, request)
