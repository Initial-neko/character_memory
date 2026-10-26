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

    @staticmethod
    def public(row):
        return {'id':row['id'],'batch_id':row['batch_id'],'suggestion_id':row['suggestion_id'],
                'kind':row['kind'],'source_ref':json.loads(row['source_ref_json']) if row['source_ref_json'] else None,
                'comment':row['comment'],'profile_version':row['profile_version'],
                'applied_profile_version':row['applied_profile_version']}

    def existing(self,request_key,request_hash):
        with self.store._lock:
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND request_key=?',(LOCAL_OPERATOR,request_key)).fetchone()
            if row is None:return None
            if row['request_hash']!=request_hash:raise SuggestionConflict('signal identity already belongs to a different payload')
            return self.public(row)

    def record(self,*,request_key,request_hash,batch_id,kind,profile_version,source_ref=None,suggestion_id=None,comment=None,applied_profile_version=None):
        with self.store.transaction(immediate=True):
            existing=self.existing(request_key,request_hash)
            if existing is not None:return existing
            identifier=str(uuid4())
            self.store.conn.execute('INSERT INTO reply_suggestion_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,LOCAL_OPERATOR,request_key,request_hash,str(batch_id),str(suggestion_id) if suggestion_id else None,
                 kind,source_ref.model_dump_json() if source_ref is not None else None,comment,profile_version,applied_profile_version,datetime.now(timezone.utc).isoformat()))
            row=self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE id=?',(identifier,)).fetchone()
            return self.public(row)

    def for_batch(self,batch_id):
        with self.store._lock:
            return [self.public(row) for row in self.store.conn.execute('SELECT * FROM reply_suggestion_signals WHERE owner_id=? AND batch_id=? ORDER BY created_at,id',(LOCAL_OPERATOR,str(batch_id)))]
