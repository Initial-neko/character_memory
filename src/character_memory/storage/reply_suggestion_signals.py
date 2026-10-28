"""Explicit local-operator signals; sent-text remains owned by its existing fact."""
import hashlib
import json
from uuid import uuid4
from datetime import datetime, timezone
from character_memory.domain.experience_profile import LOCAL_OPERATOR
from character_memory.storage.reply_suggestions import SuggestionConflict


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class ReplySuggestionSignalRepository:
    def __init__(self,store):
        self.store=store
        def migrate():
            store.conn.execute("""CREATE TABLE reply_suggestion_signals(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_key TEXT NOT NULL,
                request_hash TEXT NOT NULL, batch_id TEXT NOT NULL, suggestion_id TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('SELECT','EDIT','FREE_INPUT','IGNORE','EXPLICIT_FEEDBACK')),
                source_ref_json TEXT, comment TEXT, profile_version INTEGER NOT NULL,
                applied_profile_version INTEGER, created_at TEXT NOT NULL,
                UNIQUE(owner_id,request_key), UNIQUE(owner_id,source_ref_json))""")
        store.apply_schema_migration('experience/003-suggestion-signals',migrate,immediate=True)
        def governance():
            store.conn.execute('ALTER TABLE reply_suggestion_signals ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
            store.conn.execute('ALTER TABLE reply_suggestion_signals ADD COLUMN withdrawn_at TEXT')
            store.conn.execute('ALTER TABLE reply_suggestion_signals ADD COLUMN updated_at TEXT')
            store.conn.execute("""CREATE TABLE reply_suggestion_signal_changes(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, signal_id TEXT NOT NULL, operation TEXT NOT NULL,
                revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(owner_id,request_id))""")
        store.apply_schema_migration('experience/004-signal-governance',governance,immediate=True)


    @staticmethod
    def public(row):
        return {'id':row['id'],'batch_id':row['batch_id'],'suggestion_id':row['suggestion_id'],
                'kind':row['kind'],'source_ref':json.loads(row['source_ref_json']) if row['source_ref_json'] else None,
                'comment':row['comment'],'profile_version':row['profile_version'],
                'applied_profile_version':row['applied_profile_version'],
                'revision':row['revision'],'withdrawn':row['withdrawn_at'] is not None,
                'updated_at':row['updated_at'] or row['created_at']}

    def existing(self,request_key,request_hash):
        with self.store._lock:
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND request_key=?',(LOCAL_OPERATOR,request_key)).fetchone()
            if row is None:return None
            if row['request_hash']!=request_hash:raise SuggestionConflict('signal identity already belongs to a different payload')
            return self.public(row)

    def has_accepted_source(self,batch_id):
        with self.store._lock:
            return self.store.conn.execute(
                "SELECT 1 FROM reply_suggestion_signals WHERE owner_id=? AND batch_id=? AND kind IN ('SELECT','EDIT','FREE_INPUT') AND source_ref_json IS NOT NULL LIMIT 1",
                (LOCAL_OPERATOR,str(batch_id))).fetchone() is not None

    def record(self,*,request_key,request_hash,batch_id,kind,profile_version,source_ref=None,suggestion_id=None,comment=None,applied_profile_version=None):
        with self.store.transaction(immediate=True):
            existing=self.existing(request_key,request_hash)
            if existing is not None:return existing
            identifier=str(uuid4())
            self.store.conn.execute('INSERT INTO reply_suggestion_signals(id,owner_id,request_key,request_hash,batch_id,suggestion_id,kind,source_ref_json,comment,profile_version,applied_profile_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,LOCAL_OPERATOR,request_key,request_hash,str(batch_id),str(suggestion_id) if suggestion_id else None,
                 kind,source_ref.model_dump_json() if source_ref is not None else None,comment,profile_version,applied_profile_version,datetime.now(timezone.utc).isoformat()))
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE id=?',(identifier,)).fetchone()
            return self.public(row)

    def get(self,signal_id):
        with self.store._lock:
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND id=?',(LOCAL_OPERATOR,str(signal_id))).fetchone()
            return self.public(row) if row is not None else None

    def for_batch(self,batch_id,*,active_only=False):
        with self.store._lock:
            active=' AND withdrawn_at IS NULL' if active_only else ''
            return [self.public(row) for row in self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND batch_id=?'+active+' ORDER BY created_at,id',(LOCAL_OPERATOR,str(batch_id)))]

    @staticmethod
    def change_receipt(row):
        # No current or historical authored text belongs in a mutation receipt.
        return {'change_id':row['id'],'signal_id':row['signal_id'],'operation':row['operation'],'revision':row['revision']}

    def change(self,signal_id,operation,request):
        if operation not in {'CORRECT','WITHDRAW'}:raise ValueError('unknown signal governance operation')
        signal_id=str(signal_id)
        digest=fingerprint({'signal_id':signal_id,'operation':operation,'request':request.model_dump(mode='json')})
        with self.store.transaction(immediate=True):
            previous=self.store.conn.execute('SELECT * FROM reply_suggestion_signal_changes WHERE owner_id=? AND request_id=?',(LOCAL_OPERATOR,str(request.request_id))).fetchone()
            if previous is not None:
                if previous['request_hash']!=digest:raise SuggestionConflict('change identity already belongs to different inputs')
                return self.change_receipt(previous)
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND id=?',(LOCAL_OPERATOR,signal_id)).fetchone()
            if row is None:raise KeyError('signal not found')
            if row['revision']!=request.expected_revision:raise SuggestionConflict('signal changed; read the latest revision')
            if row['withdrawn_at'] is not None:raise SuggestionConflict('withdrawn signal cannot be revived')
            if operation=='CORRECT' and row['kind']!='EXPLICIT_FEEDBACK':raise SuggestionConflict('only explicit feedback text can be corrected')
            now=datetime.now(timezone.utc).isoformat();revision=request.expected_revision+1
            comment=request.comment if operation=='CORRECT' else None
            changed=self.store.conn.execute('UPDATE reply_suggestion_signals SET comment=?,revision=?,withdrawn_at=?,updated_at=? WHERE owner_id=? AND id=? AND revision=? AND withdrawn_at IS NULL',
                (comment,revision,now if operation=='WITHDRAW' else None,now,LOCAL_OPERATOR,signal_id,request.expected_revision))
            if changed.rowcount!=1:raise SuggestionConflict('signal changed')
            # An open interpretation follows its authored source: withdrawal
            # retires it. A still-open candidate is simply re-interpreted from a
            # corrected text. An APPLIED decision belongs to the wording it was
            # made from, so correcting that wording retires it instead of
            # re-pointing the old decision at new text.
            columns={row['name'] for row in self.store.conn.execute('PRAGMA table_info(reply_suggestion_signals)').fetchall()}
            if 'candidate_status' in columns:
                if operation=='WITHDRAW':
                    self.store.conn.execute(
                        "UPDATE reply_suggestion_signals SET candidate_status='WITHDRAWN',candidate_decided_at=? "
                        'WHERE owner_id=? AND id=? AND candidate_status IS NULL',(now,LOCAL_OPERATOR,signal_id))
                else:
                    self.store.conn.execute(
                        "UPDATE reply_suggestion_signals SET candidate_status='SUPERSEDED',candidate_decided_at=? "
                        "WHERE owner_id=? AND id=? AND candidate_status='APPLIED'",(now,LOCAL_OPERATOR,signal_id))
            identifier=str(uuid4())
            self.store.conn.execute('INSERT INTO reply_suggestion_signal_changes VALUES(?,?,?,?,?,?,?,?)',
                (identifier,LOCAL_OPERATOR,str(request.request_id),digest,signal_id,operation,revision,now))
            receipt=self.store.conn.execute('SELECT * FROM reply_suggestion_signal_changes WHERE id=?',(identifier,)).fetchone()
            return self.change_receipt(receipt)
