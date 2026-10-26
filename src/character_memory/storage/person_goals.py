"""One Core SQLite owner for Goal state, stage acceptances and mutation receipts."""
from datetime import datetime,timezone
from contextlib import contextmanager
import hashlib
import json
from uuid import uuid4
from character_memory.domain.goals import GoalCreate,GoalAction,MAX_GOAL_VERSION
from character_memory.application.goal_evidence import GoalMissing,GoalConflict,EvidenceUnavailable,evidence_digest

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()
def stamp():return datetime.now(timezone.utc).isoformat()

class GoalRepository:
    def __init__(self,store,*,initialize=True):
        self.store=store
        if initialize:store.apply_schema_migration('person-goals/001-goals',self._migrate,immediate=True)

    def _migrate(self):
        self.store.conn.execute('CREATE TABLE person_goals(id TEXT PRIMARY KEY,character_id TEXT NOT NULL,title TEXT NOT NULL,success_criteria TEXT NOT NULL,stages_json TEXT NOT NULL,current_stage INTEGER NOT NULL,phase TEXT NOT NULL CHECK(phase IN (\'ACTIVE\',\'PAUSED\',\'COMPLETED\',\'ARCHIVED\')),version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
        self.store.conn.execute('CREATE INDEX idx_person_goals_character ON person_goals(character_id,updated_at,id)')
        self.store.conn.execute('CREATE TABLE person_goal_stages(goal_id TEXT NOT NULL,stage_index INTEGER NOT NULL,evidence_json TEXT NOT NULL,note TEXT,confirmation TEXT NOT NULL,accepted_at TEXT NOT NULL,PRIMARY KEY(goal_id,stage_index))')
        self.store.conn.execute('CREATE TABLE person_goal_receipts(character_id TEXT NOT NULL,request_id TEXT NOT NULL,request_hash TEXT NOT NULL,goal_id TEXT NOT NULL,operation TEXT NOT NULL,version_applied INTEGER NOT NULL,PRIMARY KEY(character_id,request_id))')

    @contextmanager
    def _snapshot(self):
        # Keep state, stages and source statuses on one WAL read snapshot. A
        # caller-owned transaction must never be committed or rolled back here.
        with self.store._lock:
            owned=not self.store.conn.in_transaction
            if owned:self.store.conn.execute('BEGIN')
            try:
                yield
            except BaseException:
                if owned:self.store.conn.rollback()
                raise
            else:
                if owned:self.store.conn.commit()

    def _exists(self):
        return self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_goals'").fetchone() is not None

    def get(self,character_id,goal_id):
        with self._snapshot():
            if not self._exists():raise GoalMissing('GOAL_NOT_FOUND')
            row=self.store.conn.execute('SELECT * FROM person_goals WHERE id=? AND character_id=?',(goal_id,character_id)).fetchone()
            if row is None:raise GoalMissing('GOAL_NOT_FOUND')
            result=dict(row);result['stages']=json.loads(result.pop('stages_json'))
            completions=[]
            for stage in self.store.conn.execute('SELECT * FROM person_goal_stages WHERE goal_id=? ORDER BY stage_index',(goal_id,)).fetchall():
                item=dict(stage);item.pop('goal_id');evidence=json.loads(item.pop('evidence_json'))
                for source in evidence:
                    try:current=evidence_digest(self.store,character_id,source['ref'])
                    except EvidenceUnavailable:source['status']='UNAVAILABLE'
                    else:source['status']='CURRENT' if current==source['accepted_digest'] else 'CHANGED'
                item['evidence']=evidence;completions.append(item)
            result['completions']=completions
            return result

    def list(self,character_id,*,limit=50):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid goal limit')
        with self._snapshot():
            if not self._exists():return []
            ids=self.store.conn.execute('SELECT id FROM person_goals WHERE character_id=? ORDER BY updated_at DESC,id LIMIT ?',(character_id,limit)).fetchall()
            return [self.get(character_id,row['id']) for row in ids]

    def _replay(self,character_id,request_id,request_hash):
        row=self.store.conn.execute('SELECT * FROM person_goal_receipts WHERE character_id=? AND request_id=?',(character_id,request_id)).fetchone()
        if row is None:return None
        if row['request_hash']!=request_hash:raise GoalConflict('REQUEST_ID_CONFLICT')
        return self._response(row,replayed=True)

    def _response(self,row,*,replayed):
        return {'goal':self.get(row['character_id'],row['goal_id']),
                'receipt':{key:row[key] for key in ('request_id','operation','version_applied')},'replayed':replayed}

    def _record(self,character_id,request_id,request_hash,goal_id,operation,version):
        self.store.conn.execute('INSERT INTO person_goal_receipts VALUES(?,?,?,?,?,?)',(character_id,request_id,request_hash,goal_id,operation,version))
        row=self.store.conn.execute('SELECT * FROM person_goal_receipts WHERE character_id=? AND request_id=?',(character_id,request_id)).fetchone()
        return self._response(row,replayed=False)

    def create(self,character_id,request):
        request=GoalCreate.model_validate(request.model_dump(mode='json'))
        if not isinstance(character_id,str) or not character_id.strip() or len(character_id)>200:raise ValueError('invalid character identity')
        request_hash=digest({'operation':'CREATE','payload':request.model_dump(mode='json')})
        with self.store.transaction(immediate=True):
            replay=self._replay(character_id,request.request_id,request_hash)
            if replay is not None:return replay
            identifier=uuid4().hex;now=stamp()
            self.store.conn.execute('INSERT INTO person_goals VALUES(?,?,?,?,?,?,?,?,?,?)',(identifier,character_id,request.title,request.success_criteria,canonical(request.stages),0,'ACTIVE',1,now,now))
            return self._record(character_id,request.request_id,request_hash,identifier,'CREATE',1)

    def act(self,character_id,goal_id,request):
        request=GoalAction.model_validate(request.model_dump(mode='json'))
        request_hash=digest({'goal_id':goal_id,'payload':request.model_dump(mode='json')})
        with self.store.transaction(immediate=True):
            replay=self._replay(character_id,request.request_id,request_hash)
            if replay is not None:return replay
            goal=self.get(character_id,goal_id)
            if goal['version']!=request.expected_version:raise GoalConflict('GOAL_VERSION_CONFLICT')
            if goal['version']>=MAX_GOAL_VERSION:raise GoalConflict('GOAL_VERSION_EXHAUSTED')
            phase,index=goal['phase'],goal['current_stage'];operation=request.operation;now=stamp()
            if operation=='PAUSE' and phase=='ACTIVE':phase='PAUSED'
            elif operation=='RESUME' and phase=='PAUSED':phase='ACTIVE'
            elif operation=='ARCHIVE' and phase!='ARCHIVED':phase='ARCHIVED'
            elif operation=='COMPLETE_STAGE' and phase=='ACTIVE':
                evidence=[{'ref':ref.model_dump(mode='json'),'accepted_digest':evidence_digest(self.store,character_id,ref)} for ref in request.evidence]
                self.store.conn.execute('INSERT INTO person_goal_stages VALUES(?,?,?,?,?,?)',(goal_id,index,canonical(evidence),request.note,request.confirmation,now))
                index+=1
                if index==len(goal['stages']):phase='COMPLETED'
            else:raise GoalConflict('GOAL_STATE_CONFLICT')
            changed=self.store.conn.execute('UPDATE person_goals SET phase=?,current_stage=?,version=version+1,updated_at=? WHERE id=? AND character_id=? AND version=?',(phase,index,now,goal_id,character_id,request.expected_version))
            if changed.rowcount!=1:raise GoalConflict('GOAL_VERSION_CONFLICT')
            return self._record(character_id,request.request_id,request_hash,goal_id,operation,request.expected_version+1)
