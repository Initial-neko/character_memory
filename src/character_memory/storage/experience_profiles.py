"""Single durable owner for explicit experience profile revisions; no model calls."""
import json
from datetime import datetime,timezone
from pydantic import ValidationError
from character_memory.domain.experience_profile import ExperiencePreferences,PreferenceChanges,ProfileRestore,MAX_VERSION,LOCAL_OPERATOR

class ProfileConflict(ValueError):pass
class ProfileVersionMissing(LookupError):pass
class ProfileCorrupt(RuntimeError):pass

class ExperienceProfileRepository:
    def __init__(self,store,*,owner_id=LOCAL_OPERATOR,initialize=True):
        if not isinstance(owner_id,str) or not owner_id.strip() or len(owner_id)>128:raise ValueError('invalid trusted profile owner')
        self.store,self.owner_id=store,owner_id
        def migrate():
            store.conn.execute('CREATE TABLE experience_profile_heads(owner_id TEXT PRIMARY KEY,version INTEGER NOT NULL CHECK(version>0),preferences_json TEXT NOT NULL,updated_at TEXT NOT NULL)')
            store.conn.execute('CREATE TABLE experience_profile_versions(owner_id TEXT NOT NULL,version INTEGER NOT NULL,operation TEXT NOT NULL,preferences_json TEXT NOT NULL,previous_version INTEGER NOT NULL,at TEXT NOT NULL,PRIMARY KEY(owner_id,version))')
        self._migrate = lambda: store.apply_schema_migration('experience/001-explicit-profile',migrate,immediate=True)
        if initialize:self._migrate()

    @staticmethod
    def _preferences(raw):
        try:
            data=json.loads(raw)
            return ExperiencePreferences.model_validate(data).model_dump(exclude_none=True)
        except (ValueError,TypeError,ValidationError) as exc:raise ProfileCorrupt('invalid stored experience preferences') from exc

    def get(self):
        with self.store._lock:
            if self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experience_profile_heads'").fetchone() is None:
                return {'version':0,'preferences':{},'updated_at':None}
            row=self.store.conn.execute('SELECT * FROM experience_profile_heads WHERE owner_id=?',(self.owner_id,)).fetchone()
            if row is None:return {'version':0,'preferences':{},'updated_at':None}
            if type(row['version']) is not int or not 0<row['version']<=MAX_VERSION+1:raise ProfileCorrupt('invalid stored profile version')
            return {'version':row['version'],'preferences':self._preferences(row['preferences_json']),'updated_at':row['updated_at']}

    def versions(self,limit=100):
        with self.store._lock:
            rows=self.store.conn.execute('SELECT * FROM experience_profile_versions WHERE owner_id=? ORDER BY version DESC LIMIT ?',(self.owner_id,min(100,max(1,int(limit))))).fetchall()
            return [{'version':r['version'],'operation':r['operation'],'preferences':self._preferences(r['preferences_json']),'previous_version':r['previous_version'],'at':r['at']} for r in rows]

    def _change(self,expected,operation,changes=None,target=None):
        if type(expected) is not int or not 0<=expected<=MAX_VERSION:raise ValueError('invalid expected_version')
        self._migrate()
        with self.store.transaction(immediate=True):
            current=self.get()
            if current['version']!=expected:raise ProfileConflict('experience profile changed; read the latest version')
            preferences=dict(current['preferences'])
            if operation=='PATCH':
                for key,value in changes.items():
                    if value is None:preferences.pop(key,None)
                    else:preferences[key]=value
            elif operation=='RESTORE':
                row=self.store.conn.execute('SELECT preferences_json FROM experience_profile_versions WHERE owner_id=? AND version=?',(self.owner_id,target)).fetchone()
                if row is None or target>expected:raise ProfileVersionMissing('profile version is unavailable or was cleared')
                preferences=self._preferences(row['preferences_json'])
            elif operation=='CLEAR':
                preferences={}
                # Clearing removes snapshots, not just the active pointer.
                self.store.conn.execute('DELETE FROM experience_profile_versions WHERE owner_id=?',(self.owner_id,))
            else:raise ValueError('unknown explicit profile operation')
            version=expected+1;now=datetime.now(timezone.utc).isoformat();raw=json.dumps(preferences,sort_keys=True)
            if expected==0:
                self.store.conn.execute('INSERT INTO experience_profile_heads VALUES(?,?,?,?)',(self.owner_id,version,raw,now))
            else:
                changed=self.store.conn.execute('UPDATE experience_profile_heads SET version=?,preferences_json=?,updated_at=? WHERE owner_id=? AND version=?',(version,raw,now,self.owner_id,expected))
                if changed.rowcount!=1:raise ProfileConflict('profile changed')
            self.store.conn.execute('INSERT INTO experience_profile_versions VALUES(?,?,?,?,?,?)',(self.owner_id,version,operation,raw,expected,now))
            return {'version':version,'preferences':preferences,'updated_at':now}

    def patch(self,expected_version,changes):
        validated=PreferenceChanges.model_validate(changes).model_dump(exclude_unset=True)
        return self._change(expected_version,'PATCH',validated)

    def restore(self,expected_version,target_version):
        request=ProfileRestore(expected_version=expected_version,target_version=target_version)
        return self._change(request.expected_version,'RESTORE',target=request.target_version)

    def clear(self,expected_version):return self._change(expected_version,'CLEAR')
