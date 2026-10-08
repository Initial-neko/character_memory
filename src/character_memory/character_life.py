"""Read-only projections of durable character experiences; no runtime or model calls."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json

from character_memory.time_utils import epoch_us, parse_datetime


def obj(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def text(value, maximum=2000):
    return value[:maximum] if isinstance(value, str) else ""


class CharacterLifeReader:
    def __init__(self, store):
        self.store = store

    def _tables(self):
        return {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def timeline(self, character_id, *, day=None, offset=480, channel="ALL", view="life", query="", peer="", cursor="", limit=30):
        limit = max(1, min(50, limit))
        boundary = None
        if cursor:
            try:
                boundary = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(boundary, list) or len(boundary) != 2 or not isinstance(boundary[0], int) or not isinstance(boundary[1], str):
                    raise ValueError()
            except Exception as exc:
                raise ValueError("无效分页游标") from exc
        start = end = None
        if day:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(minutes=offset)))
            end = start + timedelta(days=1)
        with self.store._lock:
            tables = self._tables()
            sources = []
            # Source event identities are namespaced; Group IDs are not Core event IDs.
            sources.append(("events", "e.id", "e.event_time_epoch", "e.event_time", "e.content||CASE WHEN json_valid(e.metadata_json) THEN coalesce(json_extract(e.metadata_json,'$.rss_reading.title'),'') ELSE '' END", "e.character_id=? AND e.event_type NOT IN ('ACTION','TIME_TICK','LIFE_EVENT','DIARY')", [character_id], "SELECT e.* FROM events e"))
            if 'conversation_events' in tables:
                sources.append(("group", "e.id", "e.event_time_epoch", "e.event_time", "e.content", "(e.actor_type='CHARACTER' AND e.actor_id=? OR EXISTS(SELECT 1 FROM conversation_runtime_traces t WHERE t.character_id=? AND t.source_conversation_event_id=e.id))", [character_id, character_id], "SELECT e.* FROM conversation_events e"))
            if 'space_posts' in tables:
                sources.append(("post", "e.id", "e.created_at_epoch", "e.created_at", "e.content", "e.character_id=?", [character_id], "SELECT e.* FROM space_posts e"))
            if 'space_comments' in tables:
                sources.append(("comment", "e.id", "e.created_at_epoch", "e.created_at", "e.content", "e.actor_type='CHARACTER' AND e.character_id=?", [character_id], "SELECT e.*,p.character_id AS post_author FROM space_comments e JOIN space_posts p ON p.id=e.post_id"))
            if 'world_browse_decisions' in tables:
                sources.append(("world", "e.opportunity_id", "CAST((julianday(e.started_at)-2440587.5)*86400000000 AS INTEGER)", "e.started_at", "CASE WHEN json_valid(e.result_json) THEN coalesce(json_extract(e.result_json,'$.query'),'') ELSE '' END||e.error", "e.character_id=?", [character_id], "SELECT e.* FROM world_browse_decisions e"))
            if 'space_opportunity_runs' in tables:
                sources.append(("space_run", "e.id", "e.started_at_epoch", "e.started_at", "e.status||e.error", "e.character_id=?", [character_id], "SELECT e.* FROM space_opportunity_runs e"))
            if 'intent_deferral_audit' in tables:
                sources.append(("intent_audit", "e.id", "CAST((julianday(e.decided_at)-2440587.5)*86400000000 AS INTEGER)", "e.decided_at", "i.content||e.decision", "i.character_id=?", [character_id], "SELECT e.*,i.content,i.character_id FROM intent_deferral_audit e JOIN intents i ON i.id=e.intent_id"))
            sources.append(("intent", "e.id", "e.created_at_epoch", "e.created_at", "e.content", "e.character_id=?", [character_id], "SELECT e.* FROM intents e"))
            records = []
            for kind, identity, stamp, time, content, condition, params, sql in sources:
                if channel != 'ALL' and kind != 'events' and {'group':'GROUP','post':'SPACE','comment':'SPACE','world':'WORLD','intent':'INTENT','space_run':'SPACE','intent_audit':'INTENT'}[kind] != channel:
                    continue
                if view == 'intents' and kind != 'intent':
                    continue
                if view != 'intents' and kind == 'intent':
                    # Actual resolution history appears separately below; a plan is not a performed activity.
                    continue
                if channel != 'ALL' and kind == 'events':
                    if channel == 'WORLD': condition += " AND e.event_type='WORLD_OBSERVATION'"
                    elif channel == 'SPACE': condition += " AND e.event_type IN ('SPACE_POST_SEEN','SPACE_COMMENT_RECEIVED','SOCIAL_POST')"
                    elif channel == 'DIRECT': condition += " AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE','PROACTIVE_INTENT','VISUAL_OBSERVATION')"
                    else: continue
                if start:
                    condition += f" AND {stamp}>=? AND {stamp}<?"
                    params += [epoch_us(start), epoch_us(end)]
                if boundary:
                    condition += f" AND ({stamp}<? OR ({stamp}=? AND ?||CAST({identity} AS TEXT)<?))"
                    params += [boundary[0], boundary[0], kind+':', boundary[1]]
                if query:
                    condition += f" AND instr(lower({content}),lower(?))>0"
                    params += [query]
                if view == 'relationships':
                    if kind=='events': condition += " AND (e.event_type='USER_MESSAGE' OR (e.event_type='SPACE_COMMENT_RECEIVED' AND json_valid(e.metadata_json) AND json_extract(e.metadata_json,'$.commenter_id') IS NOT NULL))"
                    elif kind=='group': condition += " AND e.actor_type IN ('CHARACTER','USER') AND NOT(e.actor_type='CHARACTER' AND e.actor_id=?)"; params += [character_id]
                    elif kind=='comment': condition += " AND p.character_id<>?"; params += [character_id]
                    else: continue
                    if peer:
                        if kind=='events':
                            condition += " AND ((e.event_type='USER_MESSAGE' AND ?='user') OR (json_valid(e.metadata_json) AND json_extract(e.metadata_json,'$.commenter_id')=?))"; params += [peer,peer]
                        elif kind=='group': condition += " AND e.actor_id=?"; params += [peer]
                        elif kind=='comment': condition += " AND p.character_id=?"; params += [peer]
                if view == 'changes':
                    if kind == 'events':
                        condition += " AND (EXISTS(SELECT 1 FROM mental_state_history s WHERE s.character_id=e.character_id AND s.source_event_id=e.id) OR EXISTS(SELECT 1 FROM memories m WHERE m.character_id=e.character_id AND m.source_event_id=e.id))"
                    elif kind == 'group':
                        condition += " AND EXISTS(SELECT 1 FROM conversation_runtime_traces t WHERE t.character_id=? AND t.source_conversation_event_id=e.id AND json_valid(t.trace_json) AND (json_extract(t.trace_json,'$.mental_state_before')<>json_extract(t.trace_json,'$.mental_state_after') OR json_array_length(json_extract(t.trace_json,'$.created_memory_ids'))>0))"
                        params += [character_id]
                    else: continue
                condition += f" AND {stamp} IS NOT NULL"
                rows = self.store.conn.execute(sql.replace("SELECT e.*", f"SELECT {stamp} AS _stamp,e.*") + f" WHERE {condition} ORDER BY {stamp} DESC,CAST({identity} AS TEXT) DESC LIMIT ?", params+[limit+1]).fetchall()
                for row in rows:
                    item = self._project(kind, dict(row), character_id, tables)
                    item['_stamp'] = row['_stamp']
                    records.append(item)
            records.sort(key=lambda x:(x['_stamp'],x['key']),reverse=True)
            page=records[:limit]
            next_cursor = base64.urlsafe_b64encode(json.dumps([page[-1]['_stamp'],page[-1]['key']]).encode()).decode() if len(records)>limit and page else None
            for item in page: item.pop('_stamp')
            return {'character_id':character_id,'items':page,'next_cursor':next_cursor,'read_only':True}

    def _project(self, kind, row, character, tables):
        metadata=obj(row.get('metadata_json'))
        event_id=row.get('id') if kind=='events' else None
        trace={}
        if kind=='events':
            t=self.store.get_runtime_trace(event_id)
            trace=t or {}
        elif kind=='group':
            t=self.store.conn.execute("SELECT trace_json FROM conversation_runtime_traces WHERE character_id=? AND source_conversation_event_id=?",[character,row['id']]).fetchone()
            trace=obj(t[0]) if t else {}
        title={'events':'聊天经历','group':'群聊交流','post':'发布了 Space 动态','comment':'参与了 Space 讨论','world':'World 行动机会','intent':'未来表达意图','space_run':'Space 自主机会','intent_audit':'表达意图处理记录'}[kind]
        channel={'events':'DIRECT','group':'GROUP','post':'SPACE','comment':'SPACE','world':'WORLD','intent':'INTENT','space_run':'SPACE','intent_audit':'INTENT'}[kind]
        status='RECORDED'
        content=text(row.get('content'))
        participants=[]
        if kind=='events':
            et=row['event_type']
            if et=='WORLD_OBSERVATION':
                title='保留了一次外界观察';channel='WORLD'
                reading=obj(metadata.get('rss_reading'))
                if text(reading.get('title')).strip(): title='阅读了 '+text(reading['title'],180)
            elif et.startswith('SPACE_') or et=='SOCIAL_POST':
                channel='SPACE';title='Space 互动经历'
                if et=='SPACE_COMMENT_RECEIVED' and isinstance(metadata.get('commenter_id'),str):
                    participants=[{'id':metadata['commenter_id'],'name':metadata['commenter_id']}]
            elif et=='PROACTIVE_INTENT': title='主动表达机会';channel='INTENT'
            elif et=='USER_MESSAGE': participants=[{'id':'user','name':'用户'}];title='与用户交流'
            elif et=='CHARACTER_MESSAGE': title='表达了一条消息'
        if kind=='group':
            if row['actor_type'] not in ('USER','CHARACTER'): title='群聊自主机会'
            participants=[{'id':row['actor_id'],'name':'用户' if row['actor_type']=='USER' else row['actor_id']}]
        if kind=='comment' and row.get('post_author')!=character:
            participants=[{'id':row['post_author'],'name':row['post_author']}]
        state=None
        if event_id:
            history=self.store.conn.execute("SELECT * FROM mental_state_history WHERE character_id=? AND source_event_id=? ORDER BY updated_at_epoch,id",[character,event_id]).fetchall()
            if history:
                before=self.store.conn.execute("SELECT content FROM mental_state_history WHERE character_id=? AND (updated_at_epoch<? OR(updated_at_epoch=? AND id<?)) ORDER BY updated_at_epoch DESC,id DESC LIMIT 1",[character,history[0]['updated_at_epoch'],history[0]['updated_at_epoch'],history[0]['id']]).fetchone()
                state={'before':before[0] if before else None,'after':history[-1]['content']}
        if not state and (trace.get('mental_state_updated') or (kind=='group' and isinstance(trace.get('mental_state_before'),str) and isinstance(trace.get('mental_state_after'),str) and trace['mental_state_before']!=trace['mental_state_after'])):
            state={'before':text(trace.get('mental_state_before')) or None,'after':text(trace.get('mental_state_after')) or None}
        memories=[]
        if event_id:
            rows=self.store.conn.execute("SELECT id,content,active,superseded_by FROM memories WHERE character_id=? AND source_event_id=? ORDER BY id LIMIT 50",[character,event_id]).fetchall()
            memories=[dict(r) for r in rows]
        elif kind=='group':
            rows=self.store.conn.execute("SELECT id,content,active,superseded_by FROM memories WHERE character_id=? AND json_extract(metadata_json,'$.origin')='GROUP' AND CAST(json_extract(metadata_json,'$.source_conversation_event_id') AS INTEGER)=? AND json_extract(metadata_json,'$.conversation_id')=? LIMIT 50",[character,row['id'],row['conversation_id']]).fetchall()
            memories=[dict(r) for r in rows]
        decision={'basis':text(trace.get('reaction')) or None,'result':None,'error':None}
        if trace:
            actions=trace.get('actions')
            if isinstance(actions,list):
                types=[text(a.get('type'),50) for a in actions if isinstance(a,dict)]
                decision['result']='、'.join(types) or '本次机会未对外表达'
                if not types or all(t in ('NO_REPLY','NO_ACTION') for t in types): status='NO_ACTION'
        reading=obj(metadata.get('rss_reading'))
        if kind=='world':
            plan=obj(row.get('plan_json')); result=obj(row.get('result_json'))
            choice=obj(plan.get('decision')).get('choice')
            status=result.get('execution_status') or row['phase']
            title='本次 World 机会未行动' if choice=='NO_ACTION' else 'World 行动机会'
            content=text(result.get('query')) or (f'已记录选择：{choice}' if choice else '决策记录尚未完成')
            decision={'basis':None,'result':text(status,80),'error':text(row.get('error')) or None}
            event_id=row.get('source_event_id')
        if kind=='space_run':
            status=row['status']
            title='本次 Space 机会未发动态' if status=='NO_POST' else 'Space 自主机会'
            content='存在持久化的自主机会记录'
            decision={'basis':None,'result':text(status,80),'error':text(row.get('error')) or None}
        if kind=='intent_audit':
            status=row['decision']
            title='推迟了一次表达意图' if status=='DEFERRED' else '处理了一次表达意图'
            decision={'basis':None,'result':status,'error':None}
        audit=[]
        due=False
        past_expiry=False
        if kind=='intent':
            status=row['status'];content=text(row['content']);decision['basis']=text(row.get('reason')) or None
            now=datetime.now(timezone.utc)
            try:
                earliest=parse_datetime(row['earliest_at']) if row.get('earliest_at') else None
                expiry=parse_datetime(row['expires_at']) if row.get('expires_at') else None
                due=status=='PENDING' and earliest is not None and earliest<=now and (expiry is None or expiry>now)
                past_expiry=status=='PENDING' and expiry is not None and expiry<=now
            except (ValueError,TypeError):
                pass
            if 'intent_deferral_audit' in tables:
                audit=[dict(r) for r in self.store.conn.execute("SELECT decided_at,decision,old_earliest_at,new_earliest_at,source_event_id FROM intent_deferral_audit WHERE intent_id=? ORDER BY id LIMIT 50",[row['id']])]
        return {'key':kind+':'+str(row.get('opportunity_id',row.get('id'))),'kind':kind,'time':row.get('event_time',row.get('created_at',row.get('started_at',row.get('decided_at')))),'title':title,'channel':channel,'status':status,'content':content,'source':{'kind':kind,'id':row.get('opportunity_id',row.get('id')),'event_id':event_id,'conversation_id':row.get('conversation_id'),'post_id':row.get('post_id') or metadata.get('post_id')},'decision':decision,'state_change':state,'memories':memories,'participants':participants,'reading':{'scope':text(reading.get('content_scope'),80),'url':text(reading.get('url'),1000),'summary':text(metadata.get('world_summary'))} if reading else None,'intent':{'earliest_at':row.get('earliest_at'),'expires_at':row.get('expires_at'),'audit':audit,'due':due,'past_expiry':past_expiry,'defer_count':row.get('deferral_count',0)} if kind=='intent' else None}
