"""Durable claim for a user-requested draft batch; unknown work is never replayed."""
import json
from datetime import datetime, timezone
from uuid import uuid4
from character_memory.domain.experience_profile import LOCAL_OPERATOR

class SuggestionConflict(ValueError):
    pass

class ReplySuggestionRepository:
    def __init__(self, store, *, owner_id=LOCAL_OPERATOR):
        self.store, self.owner_id = store, owner_id
        def migrate():
            store.conn.execute("""CREATE TABLE reply_suggestion_batches(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_id TEXT NOT NULL,
                request_json TEXT NOT NULL, context_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','READY','FAILED','STALE')),
                suggestions_json TEXT, failure_json TEXT, created_at TEXT NOT NULL,
                UNIQUE(owner_id,request_id))""")
        store.apply_schema_migration('experience/002-reply-suggestions', migrate, immediate=True)

    def by_request(self, request_id):
        with self.store._lock:
            row = self.store.conn.execute(
                'SELECT * FROM reply_suggestion_batches WHERE owner_id=? AND request_id=?',
                (self.owner_id, str(request_id))).fetchone()
            return dict(row) if row else None

    def get(self, batch_id):
        with self.store._lock:
            row = self.store.conn.execute('SELECT * FROM reply_suggestion_batches WHERE id=? AND owner_id=?',
                                          (str(batch_id), self.owner_id)).fetchone()
            return dict(row) if row else None

    def claim(self, request, digest):
        payload = request.model_dump_json()
        with self.store.transaction(immediate=True):
            existing = self.by_request(request.request_id)
            if existing:
                if existing['request_json'] != payload:
                    raise SuggestionConflict('request identity already used for different inputs')
                return existing, False
            batch_id = str(uuid4())
            self.store.conn.execute('INSERT INTO reply_suggestion_batches VALUES(?,?,?,?,?,?,?,?,?)',
                (batch_id, self.owner_id, str(request.request_id), payload, digest,
                 'PENDING', None, None, datetime.now(timezone.utc).isoformat()))
            return self.get(batch_id), True

    def finish(self, batch_id, status, *, suggestions=None, failure=None):
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "UPDATE reply_suggestion_batches SET status=?,suggestions_json=?,failure_json=? "
                "WHERE id=? AND owner_id=? AND status='PENDING'",
                (status, json.dumps(suggestions, ensure_ascii=False) if suggestions is not None else None,
                 json.dumps(failure, ensure_ascii=False) if failure is not None else None, batch_id, self.owner_id))

    def stale(self, batch_id):
        with self.store.transaction(immediate=True):
            self.store.conn.execute("UPDATE reply_suggestion_batches SET status='STALE',suggestions_json=NULL WHERE id=? AND owner_id=?",
                                    (batch_id, self.owner_id))
