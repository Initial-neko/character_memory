"""Scene-day projection from existing Ensemble and shared Group event facts."""
from datetime import datetime
import json

from character_memory.domain.experience_scope import real_experience_sql
from character_memory.group_store import GroupEvent, GroupRepository


class GroupScenes:
    def __init__(self,store): self.store=store

    def _template(self,group_id):
        if not self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='ensemble_builds'").fetchone(): return None
        row=self.store.conn.execute("SELECT drafts_json FROM ensemble_builds WHERE group_id=? AND status='ACTIVE'",(group_id,)).fetchone()
        if row is None: return None
        drafts=json.loads(row['drafts_json'] or '[]')
        return 'warm-isekai' if drafts and all(d.get('template_id')=='warm-isekai' for d in drafts) else None

    def state(self,group_id,*,max_event_id=None):
        with self.store._lock:
            template=self._template(group_id)
            if template is None: return None
            args=[group_id]
            bound=''
            if max_event_id is not None:
                bound=' AND id<=?'
                args.append(max_event_id)
            row=self.store.conn.execute("SELECT id,metadata_json FROM conversation_events WHERE conversation_id=? "
                "AND event_type='SCENE_DAY_ADVANCED' AND "+real_experience_sql("metadata_json")+bound+' ORDER BY id DESC LIMIT 1',args).fetchone()
            data=json.loads(row['metadata_json']) if row else {}
            day=int(data.get('to_day',1))
            return {'template_id':template,'story_day':day,'phase':'EVENING' if day==1 else 'MORNING',
                    'last_control_event_id':row['id'] if row else None,'user_controlled':True,
                    'scope':'GROUP_STORY_ONLY'}

    def resume(self,template_id):
        if template_id!='warm-isekai': raise KeyError('unknown scene template')
        with self.store._lock:
            rows=self.store.conn.execute("SELECT group_id,status,drafts_json FROM ensemble_builds "
                "WHERE status IN ('ACTIVE','READY') AND json_extract(drafts_json,'$[0].template_id')=? "
                "ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END,updated_at_epoch DESC",(template_id,)).fetchall()
            for row in rows:
                if row['status']=='ACTIVE' and self.store.conn.execute('SELECT 1 FROM conversations WHERE id=? AND archived_at IS NULL',(row['group_id'],)).fetchone() is None:
                    continue
                return {'group_id':row['group_id'],'status':row['status'],'scene':self.state(row['group_id'])}
        return None

    def advance(self,group_id,*,expected_day,request_id,now=None):
        with self.store.transaction(immediate=True):
            state=self.state(group_id)
            group=GroupRepository(self.store).get_group(group_id)
            if state is None or group is None: raise KeyError('active scene group not found')
            previous=self.store.conn.execute("SELECT metadata_json FROM conversation_events WHERE conversation_id=? "
                "AND event_type='SCENE_DAY_ADVANCED' AND "+real_experience_sql("metadata_json")+" AND json_extract(metadata_json,'$.request_id')=?",(group_id,request_id)).fetchone()
            if previous:
                if json.loads(previous['metadata_json']).get('from_day')!=expected_day: raise ValueError('request_id conflict')
                return state
            if expected_day!=state['story_day']: raise ValueError('scene day changed; reload before advancing')
            GroupRepository(self.store).append_event(GroupEvent(conversation_id=group_id,turn_id=f'scene:{request_id}',
                actor_type='SYSTEM',actor_id='scene-control',event_type='SCENE_DAY_ADVANCED',
                event_time=now or datetime.now().astimezone(),content=f'用户将场景推进到第{expected_day+1}天早晨。没有记录期间发生的共同经历。',
                metadata={'source':'USER_ACTION','request_id':request_id,'from_day':expected_day,'to_day':expected_day+1,'hidden':True}))
            return self.state(group_id)
