"""Durable decisions for interpreted preference candidates.

Candidates themselves are derived deterministically from authored EXPLICIT_FEEDBACK
text; only the operator's decision has to be durable. Decisions live as extra
columns on the existing `reply_suggestion_signals` table (no new table), and every
effective preference write still goes through ExperienceProfileRepository CAS.
"""
from datetime import datetime, timezone
from character_memory.domain.experience_profile import LOCAL_OPERATOR
from character_memory.storage.reply_suggestion_signals import ReplySuggestionSignalRepository

STORED_STATUSES = ('APPLIED', 'DISMISSED', 'SUPERSEDED', 'WITHDRAWN', 'REVERTED')


class CandidateConflict(ValueError):
    pass


class CandidateMissing(LookupError):
    pass


class PreferenceCandidateRepository:
    def __init__(self, store, *, owner_id=LOCAL_OPERATOR):
        self.store = store
        self.owner_id = owner_id
        # Create/own the signal table before extending it, so a fresh database
        # never depends on route/module import order.
        ReplySuggestionSignalRepository(store)
        def migrate():
            columns = {row['name'] for row in store.conn.execute('PRAGMA table_info(reply_suggestion_signals)').fetchall()}
            for name, declaration in (('candidate_status', 'TEXT'), ('candidate_applied_version', 'INTEGER'),
                                      ('candidate_base_version', 'INTEGER'), ('candidate_request_id', 'TEXT'),
                                      ('candidate_decided_at', 'TEXT')):
                if name not in columns:
                    store.conn.execute(f'ALTER TABLE reply_suggestion_signals ADD COLUMN {name} {declaration}')
        self._migrate = lambda: store.apply_schema_migration('experience/005-candidate-decisions', migrate, immediate=True)
        self._migrate()

    def sources(self, limit=100):
        """Authored feedback that may carry an interpretation, newest first."""
        with self.store._lock:
            self._migrate()
            rows = self.store.conn.execute(
                "SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND kind='EXPLICIT_FEEDBACK' "
                "AND ((comment IS NOT NULL AND TRIM(comment)<>'') OR candidate_status IS NOT NULL) "
                'ORDER BY created_at DESC,id DESC LIMIT ?',
                (self.owner_id, min(100, max(1, int(limit))))).fetchall()
            return [dict(row) for row in rows]

    def decision(self, signal_id):
        with self.store._lock:
            self._migrate()
            row = self.store.conn.execute(
                'SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND id=? AND kind=\'EXPLICIT_FEEDBACK\'',
                (self.owner_id, str(signal_id))).fetchone()
            return dict(row) if row is not None else None

    def _decide(self, signal_id, *, request_id, status, applied_version=None, base_version=None):
        if status not in STORED_STATUSES:
            raise ValueError('unknown candidate decision')
        self._migrate()
        with self.store.transaction(immediate=True):
            row = self.decision(signal_id)
            if row is None:
                raise CandidateMissing('candidate source is unavailable')
            if row['candidate_request_id'] == str(request_id):
                if row['candidate_status'] != status or row['candidate_applied_version'] != applied_version:
                    raise CandidateConflict('candidate request identity already belongs to a different decision')
                return row
            if row['candidate_status'] is not None:
                raise CandidateConflict('candidate was already decided; read the current profile')
            if row['withdrawn_at'] is not None:
                raise CandidateConflict('candidate source was withdrawn')
            now = datetime.now(timezone.utc).isoformat()
            changed = self.store.conn.execute(
                'UPDATE reply_suggestion_signals SET candidate_status=?,candidate_applied_version=?,candidate_base_version=?,'
                'candidate_request_id=?,candidate_decided_at=? '
                'WHERE owner_id=? AND id=? AND candidate_status IS NULL AND withdrawn_at IS NULL',
                (status, applied_version, base_version, str(request_id), now, self.owner_id, str(signal_id)))
            if changed.rowcount != 1:
                raise CandidateConflict('candidate changed')
            return self.decision(signal_id)

    def mark_applied(self, signal_id, *, request_id, applied_version, base_version):
        return self._decide(signal_id, request_id=request_id, status='APPLIED',
                            applied_version=applied_version, base_version=base_version)

    def mark_dismissed(self, signal_id, *, request_id):
        return self._decide(signal_id, request_id=request_id, status='DISMISSED')

    def mark_noop(self, signal_id, *, request_id):
        # Nothing to write because the explicit choice already says this; keep the
        # fact that the operator saw and accepted the interpretation.
        return self._decide(signal_id, request_id=request_id, status='SUPERSEDED')

    def mark_reverted(self, signal_id, *, request_id, reverted_version):
        with self.store.transaction(immediate=True):
            row = self.decision(signal_id)
            if row is None:
                raise CandidateMissing('candidate source is unavailable')
            if row['candidate_request_id'] == str(request_id) and row['candidate_status'] == 'REVERTED':
                return row
            if row['candidate_status'] != 'APPLIED':
                raise CandidateConflict('only an applied candidate can be reverted')
            now = datetime.now(timezone.utc).isoformat()
            changed = self.store.conn.execute(
                "UPDATE reply_suggestion_signals SET candidate_status='REVERTED',candidate_applied_version=?,"
                'candidate_request_id=?,candidate_decided_at=? '
                "WHERE owner_id=? AND id=? AND candidate_status='APPLIED'",
                (reverted_version, str(request_id), now, self.owner_id, str(signal_id)))
            if changed.rowcount != 1:
                raise CandidateConflict('candidate changed')
            return self.decision(signal_id)

    def mark_source_withdrawn(self, signal_id):
        """Withdrawing the authored feedback also retires its open interpretation."""
        with self.store.transaction(immediate=True):
            self._migrate()
            now = datetime.now(timezone.utc).isoformat()
            self.store.conn.execute(
                "UPDATE reply_suggestion_signals SET candidate_status='WITHDRAWN',candidate_decided_at=? "
                'WHERE owner_id=? AND id=? AND candidate_status IS NULL',
                (now, self.owner_id, str(signal_id)))

    def supersede_all(self):
        """Clearing the profile expires every undecided interpretation.

        Without this, an old comment would offer the cleared value back on the
        next read, which would look like a cleared preference coming back.
        """
        with self.store.transaction(immediate=True):
            self._migrate()
            now = datetime.now(timezone.utc).isoformat()
            self.store.conn.execute(
                "UPDATE reply_suggestion_signals SET candidate_status='SUPERSEDED',candidate_decided_at=? "
                "WHERE owner_id=? AND kind='EXPLICIT_FEEDBACK' AND candidate_status IS NULL",
                (now, self.owner_id))
