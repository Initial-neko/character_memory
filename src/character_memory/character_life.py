"""Read-only projections of durable character experiences; no runtime or model calls."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from character_memory.time_utils import epoch_us, parse_datetime


def obj(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def text(value, maximum=2000):
    return value[:maximum] if isinstance(value, str) else ""


def continuation_life_projection(row, plan, result):
    """Bounded public facts for the existing Life body, never raw model context."""
    labels={'SUCCESS':'成功返回', 'FAILED':'失败', 'DENIED':'未获许可',
            'UNKNOWN':'结果未知', 'SKIPPED':'未执行或结果未知', 'STARTED':'尚无结果',
            'TIMEOUT':'超时', 'NO_ACTION':'未行动', 'PENDING':'尚未完成'}
    raw=result.get('steps', plan.get('steps', []))
    unverified=not isinstance(raw,list)
    if isinstance(raw,list) and any(not isinstance(step,dict) for step in raw[:3]):
        unverified=True
    raw=[step for step in raw[:3] if isinstance(step, dict)] if isinstance(raw, list) else []
    pending=obj(plan.get('pending_step'))
    if pending and not any(step.get('request_id')==pending.get('request_id') for step in raw):
        raw.append({**pending, 'status':'UNKNOWN'})
    steps, sources=[], []
    for entry in raw[:3]:
        if entry.get('character_id')!=row['character_id'] or entry.get('opportunity_id')!=row['opportunity_id']:
            unverified=True
            continue
        capability=text(entry.get('capability'),64)
        step={'request_id':text(entry.get('request_id'),160), 'capability':capability,
              'status':text(entry.get('status'),32) or 'UNKNOWN', 'reason':text(entry.get('reason'),240)}
        step_sources=[]
        data=obj(entry.get('data'))
        if step['status']=='SUCCESS':
            if capability in ('BROWSER_OPEN','BROWSER_INSPECT','BROWSER_FOLLOW_LINK'):
                observation=obj(data.get('observation'))
                rows=[{'url':observation.get('final_url'),'content':observation.get('text')}]
                scope='BROWSER_RENDERED_TEXT'
            elif capability in ('WEB_SEARCH','READ_RSS'):
                rows=data.get('observations' if capability=='WEB_SEARCH' else 'items', [])
                scope='WEB_RENDERED_TEXT' if capability=='WEB_SEARCH' else 'RSS_FEED_TEXT'
            else:
                rows=[]; scope=''
            for source in rows[:6] if isinstance(rows,list) else []:
                if not isinstance(source,dict) or not isinstance(source.get('content'),str) or not source['content'].strip():
                    continue
                url=text(source.get('url'),4096)
                if not url.startswith(('https://','http://')):
                    continue
                step_sources.append({'url':url,'content_scope':scope,'read_characters':len(source['content'])})
        step['sources']=step_sources
        sources.extend(step_sources)
        steps.append(step)
    phase=row['phase']
    envelope=text(result.get('execution_status'),32)
    if envelope in ('FAILED','UNKNOWN','TIMEOUT'):
        status=envelope
    elif unverified or any(step['status'] in ('UNKNOWN','SKIPPED','STARTED') for step in steps):
        status='UNKNOWN'
    elif phase!='APPLIED':
        status='PENDING'
    elif not steps:
        status='NO_ACTION'
    elif any(step['status'] in ('FAILED','TIMEOUT') for step in steps):
        status='FAILED'
    elif any(step['status']=='DENIED' for step in steps):
        status='DENIED'
    else:
        status='SUCCESS' if all(step['status']=='SUCCESS' for step in steps) else 'UNKNOWN'
    stop_reason=text(result.get('stop_reason') or plan.get('stop_reason'),120)
    title={'NO_ACTION':'本次 World 机会未行动','UNKNOWN':'World 自主行动结果未知',
           'PENDING':'World 自主机会尚未完成','SUCCESS':'World 自主行动已结束'}.get(status,'World 自主行动未全部成功')
    lines=[f'已记录工具请求 {len(steps)} 步；结果：{labels.get(status,status)}。']
    for index, step in enumerate(steps,1):
        lines.append(f"步骤 {index}：{step['capability']} · {labels.get(step['status'],'结果未知')}；请求 {step['request_id']}")
        if step['reason']: lines.append('回执说明：'+step['reason'])
        if step['capability']=='BROWSER_SCREENSHOT':
            lines.append('截图生成不代表已经作为视觉输入提供给人物。')
        for source in step['sources']:
            lines.append(f"来源：{source['url']} · {source['content_scope']} · 实际提供 {source['read_characters']} 字符")
    if status=='PENDING':
        lines.append('没有机会完成记录；不能据此判断仍在运行或已经中断。')
    if status=='UNKNOWN':
        lines.append('未知结果不能当作执行成功，也不会由查看记录触发重试。')
    kept=result.get('kept') is True and bool(sources)
    lines.append('保留个人观察：'+('是' if kept else '否'))
    if sources and text(result.get('summary'),1400):
        lines.append('人物评估摘要（非原文）：'+text(result.get('summary'),1400))
    details={'steps':steps,'sources':sources,'stop_reason':stop_reason,'kept':kept,
             'phase':phase,'execution_status':envelope or None}
    decision={'basis':('停止原因：'+stop_reason) if stop_reason else None,
              'result':labels.get(status,status),'error':text(row.get('error'),500) or None,
              'execution_request_id':None}
    return title,status,'\n'.join(lines),decision,details


def collaboration_peer_sql(channel, *, match_peer=False):
    """Correlate a shared fact to this person's durable send/receive receipt.

    The SQL expressions are internal constants, never request input. Matching a
    channel, owner, fact and actor prevents numeric IDs in other channels/posts
    from making an unrelated fact visible. ACTION rows themselves stay hidden.
    """
    if channel not in ('GROUP', 'SPACE'):
        raise ValueError('invalid collaboration channel')
    metadata = "CASE WHEN json_valid(c.metadata_json) THEN c.metadata_json ELSE '{}' END"
    def field(name):
        return f"json_extract({metadata},'$.{name}')"
    actor = 'e.actor_id' if channel == 'GROUP' else 'e.character_id'
    channel_id = 'e.conversation_id' if channel == 'GROUP' else 'CAST(e.post_id AS TEXT)'
    peer = f"CASE WHEN {field('actor_id')}=c.character_id THEN {field('recipient_id')} ELSE {field('actor_id')} END"
    return (f"SELECT {peer} FROM events c WHERE c.character_id=? AND c.event_type='ACTION' "
        f"AND {field('fact_kind')}='ROLE_COLLABORATION_MESSAGE' AND {field('channel')}='{channel}' "
        f"AND {field('channel_id')}={channel_id} AND json_type({metadata},'$.shared_fact_id')='integer' "
        f"AND {field('shared_fact_id')}=e.id AND e.actor_type='CHARACTER' AND {field('actor_id')}={actor} "
        f"AND (({field('direction')}='SENT' AND {field('actor_id')}=c.character_id) "
        f"OR ({field('direction')}='RECEIVED' AND {field('recipient_id')}=c.character_id)) "
        f"AND {field('actor_id')}<>{field('recipient_id')}" + (f" AND ({peer})=?" if match_peer else ''))


class CharacterExecutionReader:
    """Read existing execution receipts. Never initializes schemas or models."""
    def __init__(self, store, detail_dir):
        self.store = store
        self.detail_dir = Path(detail_dir)
        # Connection-local function only: inspection never migrates the database.
        with store._lock:
            store.conn.create_function('execution_epoch_us',1,self._timestamp,deterministic=True)

    @staticmethod
    def _timestamp(value):
        try:
            return epoch_us(parse_datetime(value))
        except (ValueError,TypeError,OverflowError):
            return None

    def _exists(self):
        return bool(self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capability_executions'").fetchone())

    @staticmethod
    def _project(row):
        row = dict(row)
        request = obj(row.get("request_json"))
        result = obj(row.get("result_json"))
        execution = obj(row.get("execution_metadata_json")) or obj(obj(result.get("data")).get("execution"))
        return {"request_id": row["request_id"], "opportunity_id": row["opportunity_id"],
            "character_id": row["character_id"], "started_at": row["started_at"], "completed_at": row.get("completed_at"),
            "status": row["status"], "capability": request.get("capability"), "backend": execution.get("backend", "direct"),
            "execution": execution, "reason": result.get("reason", ""), "task": text(obj(request.get("arguments")).get("query"), 240)}

    def list(self, character_id, *, cursor="", limit=30, day=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid limit")
        if day:
            datetime.strptime(day, "%Y-%m-%d")
        position = None
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(position, list) or len(position) != 2 or not all(isinstance(v, str) and len(v) <= 256 for v in position):
                    raise ValueError()
                if not position[1] or parse_datetime(position[0]) is None:
                    raise ValueError()
            except Exception as error:
                raise ValueError("invalid cursor") from error
        empty = {"read_only": True, "items": [], "next_cursor": None,
                 "summary": {"scope": "all_recorded_claude_tasks", "executions": 0, "known_estimated_cost_usd": 0,
                             "unknown_usage_records": 0, "input_tokens": 0, "output_tokens": 0}}
        if not self._exists():
            return empty
        where, values = "character_id=?", [character_id]
        columns = {row[1] for row in self.store.conn.execute("PRAGMA table_info(capability_executions)")}
        if day:
            where += (" AND COALESCE(json_extract(execution_metadata_json,'$.day'),substr(started_at,1,10))=?"
                      if "execution_metadata_json" in columns else " AND substr(started_at,1,10)=?")
            values.append(day)
        if position:
            where += " AND (execution_epoch_us(started_at)<? OR (execution_epoch_us(started_at)=? AND request_id<?))"
            stamp=epoch_us(parse_datetime(position[0]))
            values += [stamp,stamp,position[1]]
        rows = self.store.conn.execute(f"SELECT * FROM capability_executions WHERE {where} ORDER BY execution_epoch_us(started_at) DESC,request_id DESC LIMIT ?", [*values, limit + 1]).fetchall()
        items = [self._project(row) for row in rows[:limit]]
        next_cursor = base64.urlsafe_b64encode(json.dumps([items[-1]["started_at"], items[-1]["request_id"]]).encode()).decode() if len(rows) > limit else None
        # SQL aggregates keep the projection bounded even with long-running histories.
        summary = empty["summary"]
        if "execution_metadata_json" in columns:
            clause = " AND COALESCE(json_extract(execution_metadata_json,'$.day'),substr(started_at,1,10))=?" if day else ""
            aggregate = self.store.conn.execute("""SELECT COUNT(*) AS executions,
                COALESCE(SUM(json_extract(execution_metadata_json,'$.estimated_cost_usd')),0) AS known_estimated_cost_usd,
                COALESCE(SUM(CASE WHEN json_extract(execution_metadata_json,'$.usage_state')='COMPLETE' THEN 0 ELSE 1 END),0) AS unknown_usage_records,
                SUM(json_extract(execution_metadata_json,'$.input_tokens')) AS input_tokens,
                SUM(json_extract(execution_metadata_json,'$.output_tokens')) AS output_tokens,
                COUNT(json_extract(execution_metadata_json,'$.input_tokens')) AS input_known_records,
                COUNT(json_extract(execution_metadata_json,'$.output_tokens')) AS output_known_records
                FROM capability_executions WHERE character_id=? AND json_extract(execution_metadata_json,'$.backend')='claude'""" + clause,
                [character_id, day] if day else [character_id]).fetchone()
            summary = {"scope": "recorded_claude_tasks_for_day" if day else "all_recorded_claude_tasks", "day": day, **dict(aggregate)}
            summary["total_estimated_cost_usd"] = summary["known_estimated_cost_usd"] if not summary["unknown_usage_records"] else None
            for field in ("input", "output"):
                key=field+"_tokens"
                summary["known_"+key]=summary[key]
                if summary[field+"_known_records"] != summary["executions"]:
                    summary[key]=None
        return {"read_only": True, "items": items, "next_cursor": next_cursor, "summary": summary}

    def detail(self, character_id, request_id):
        if not self._exists():
            return None
        row = self.store.conn.execute("SELECT * FROM capability_executions WHERE character_id=? AND request_id=?", [character_id, request_id]).fetchone()
        if row is None:
            return None
        item = self._project(row)
        data = obj(obj(dict(row).get("result_json")).get("data"))
        item.update(read_only=True, assistant_response=None, source_receipts=data.get("source_receipts") or [], details_state="NOT_RECORDED")
        steps = data.get("tool_steps")
        item.update(tool_steps=[step for step in steps[:5] if isinstance(step, dict)] if isinstance(steps, list) else [],
                    tool_steps_state="RECORDED" if isinstance(steps, list) else "NOT_RECORDED",
                    tool_steps_truncated=data.get("tool_steps_truncated") is True)
        relative = item["execution"].get("detail_path")
        if relative:
            item["details_state"] = "UNAVAILABLE"
            if isinstance(relative, str) and re.fullmatch(r"[0-9a-f-]{36}\.json", relative):
                path = (self.detail_dir / relative).resolve()
                if path.is_relative_to(self.detail_dir.resolve()):
                    try:
                        if path.stat().st_size <= 512000:
                            detail = obj(path.read_text(encoding="utf-8"))
                            item["assistant_response"] = text(detail.get("assistant_response"), 12000)
                            item["source_receipts"] = detail.get("receipts") or []
                            if isinstance(detail.get("tool_steps"), list):
                                item["tool_steps"] = [step for step in detail["tool_steps"][:5] if isinstance(step, dict)]
                                item["tool_steps_state"] = "RECORDED"
                                item["tool_steps_truncated"] = detail.get("tool_steps_truncated") is True
                            item["details_state"] = "AVAILABLE"
                    except (OSError, ValueError):
                        pass
        return item


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
                sources.append(("group", "e.id", "e.event_time_epoch", "e.event_time", "e.content", f"(e.actor_type='CHARACTER' AND e.actor_id=? OR EXISTS(SELECT 1 FROM conversation_runtime_traces t WHERE t.character_id=? AND t.source_conversation_event_id=e.id) OR EXISTS({collaboration_peer_sql('GROUP')}))", [character_id, character_id, character_id], "SELECT e.* FROM conversation_events e"))
            if 'space_posts' in tables:
                sources.append(("post", "e.id", "e.created_at_epoch", "e.created_at", "e.content", "e.character_id=?", [character_id], "SELECT e.* FROM space_posts e"))
            if 'space_comments' in tables:
                sources.append(("comment", "e.id", "e.created_at_epoch", "e.created_at", "e.content", f"(e.actor_type='CHARACTER' AND e.character_id=? OR EXISTS({collaboration_peer_sql('SPACE')}))", [character_id, character_id], "SELECT e.*,CASE WHEN e.reply_to_comment_id IS NULL THEN p.character_id ELSE parent.character_id END AS peer_id FROM space_comments e JOIN space_posts p ON p.id=e.post_id LEFT JOIN space_comments parent ON parent.id=e.reply_to_comment_id AND parent.post_id=e.post_id"))
            if 'world_browse_decisions' in tables:
                sources.append(("world", "e.opportunity_id", "CAST((julianday(e.started_at)-2440587.5)*86400000000 AS INTEGER)", "e.started_at", "CASE WHEN json_valid(e.result_json) THEN coalesce(json_extract(e.result_json,'$.query'),'')||coalesce(json_extract(e.result_json,'$.summary'),'')||coalesce(json_extract(e.result_json,'$.observations'),'') ELSE '' END||e.error", "e.character_id=?", [character_id], "SELECT e.* FROM world_browse_decisions e"))
            if 'space_opportunity_runs' in tables:
                sources.append(("space_run", "e.id", "e.started_at_epoch", "e.started_at", "e.status||e.error", "e.character_id=?", [character_id], "SELECT e.* FROM space_opportunity_runs e"))
            if 'intent_deferral_audit' in tables:
                sources.append(("intent_audit", "e.id", "CAST((julianday(e.decided_at)-2440587.5)*86400000000 AS INTEGER)", "e.decided_at", "i.content||e.decision", "i.character_id=?", [character_id], "SELECT e.*,i.content,i.character_id FROM intent_deferral_audit e JOIN intents i ON i.id=e.intent_id"))
            sources.append(("intent", "e.id", "e.created_at_epoch", "e.created_at", "e.content", "e.character_id=?", [character_id], "SELECT e.* FROM intents e"))
            records = []
            undated_count = 0
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
                if query:
                    condition += f" AND instr(lower({content}),lower(?))>0"
                    params += [query]
                if view == 'relationships':
                    if kind=='events': condition += " AND (e.event_type='USER_MESSAGE' OR (e.event_type='SPACE_COMMENT_RECEIVED' AND json_valid(e.metadata_json) AND json_extract(e.metadata_json,'$.commenter_id') IS NOT NULL))"
                    elif kind=='group': condition += f" AND e.actor_type IN ('CHARACTER','USER') AND (NOT(e.actor_type='CHARACTER' AND e.actor_id=?) OR EXISTS({collaboration_peer_sql('GROUP')}))"; params += [character_id, character_id]
                    elif kind=='comment': condition += f" AND (EXISTS({collaboration_peer_sql('SPACE')}) OR (CASE WHEN e.reply_to_comment_id IS NULL THEN p.character_id ELSE parent.character_id END)<>?)"; params += [character_id, character_id]
                    else: continue
                    if peer:
                        if kind=='events':
                            condition += " AND ((e.event_type='USER_MESSAGE' AND ?='user') OR (json_valid(e.metadata_json) AND json_extract(e.metadata_json,'$.commenter_id')=?))"; params += [peer,peer]
                        elif kind=='group': condition += f" AND ((e.actor_id=? AND e.actor_id<>?) OR EXISTS({collaboration_peer_sql('GROUP', match_peer=True)}))"; params += [peer, character_id, character_id, peer]
                        elif kind=='comment': condition += f" AND (EXISTS({collaboration_peer_sql('SPACE', match_peer=True)}) OR (NOT EXISTS({collaboration_peer_sql('SPACE')}) AND (CASE WHEN e.reply_to_comment_id IS NULL THEN p.character_id ELSE parent.character_id END)=?))"; params += [character_id, peer, character_id, peer]
                if view == 'changes':
                    if kind == 'events':
                        condition += " AND (EXISTS(SELECT 1 FROM mental_state_history s WHERE s.character_id=e.character_id AND s.source_event_id=e.id) OR EXISTS(SELECT 1 FROM memories m WHERE m.character_id=e.character_id AND m.source_event_id=e.id AND CASE WHEN json_valid(m.metadata_json) THEN coalesce(upper(json_extract(m.metadata_json,'$.origin')),'') ELSE '' END<>'GROUP'))"
                    elif kind == 'group':
                        condition += " AND EXISTS(SELECT 1 FROM conversation_runtime_traces t WHERE t.character_id=? AND t.source_conversation_event_id=e.id AND json_valid(t.trace_json) AND (json_extract(t.trace_json,'$.mental_state_before')<>json_extract(t.trace_json,'$.mental_state_after') OR json_array_length(json_extract(t.trace_json,'$.created_memory_ids'))>0))"
                        params += [character_id]
                    else: continue
                invalid_time = f"({stamp} IS NULL OR julianday({time}) IS NULL)"
                undated_count += self.store.conn.execute("SELECT COUNT(*) FROM (" + sql +
                    f" WHERE {condition} AND {invalid_time})", params).fetchone()[0]
                if start:
                    condition += f" AND {stamp}>=? AND {stamp}<?"
                    params += [epoch_us(start), epoch_us(end)]
                if boundary:
                    condition += f" AND ({stamp}<? OR ({stamp}=? AND ?||CAST({identity} AS TEXT)<?))"
                    params += [boundary[0], boundary[0], kind+':', boundary[1]]
                condition += f" AND {stamp} IS NOT NULL AND julianday({time}) IS NOT NULL"
                rows = self.store.conn.execute(sql.replace("SELECT e.*", f"SELECT {stamp} AS _stamp,e.*") + f" WHERE {condition} ORDER BY {stamp} DESC,CAST({identity} AS TEXT) DESC LIMIT ?", params+[limit+1]).fetchall()
                for row in rows:
                    item = self._project(kind, dict(row), character_id, tables)
                    item['_stamp'] = row['_stamp']
                    records.append(item)
            records.sort(key=lambda x:(x['_stamp'],x['key']),reverse=True)
            page=records[:limit]
            next_cursor = base64.urlsafe_b64encode(json.dumps([page[-1]['_stamp'],page[-1]['key']]).encode()).decode() if len(records)>limit and page else None
            for item in page: item.pop('_stamp')
            return {'character_id':character_id,'items':page,'next_cursor':next_cursor,'read_only':True,'undated_count':undated_count}

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
                if text(reading.get('title')).strip(): title=('阅读了 RSS Feed 文本：' if reading.get('content_scope')=='RSS_FEED_TEXT' else '阅读了 ')+text(reading['title'],180)
            elif et.startswith('SPACE_') or et=='SOCIAL_POST':
                channel='SPACE';title='Space 互动经历'
                if et=='SPACE_COMMENT_RECEIVED' and isinstance(metadata.get('commenter_id'),str):
                    participants=[{'id':metadata['commenter_id'],'name':metadata['commenter_id']}]
            elif et=='PROACTIVE_INTENT': title='主动表达机会';channel='INTENT'
            elif et=='USER_MESSAGE': participants=[{'id':'user','name':'用户'}];title='与用户交流'
            elif et=='CHARACTER_MESSAGE': title='表达了一条消息'
        if kind=='group':
            if row['actor_type'] not in ('USER','CHARACTER'): title='群聊自主机会'
            if row['actor_type'] in ('USER','CHARACTER') and row['actor_id']!=character:
                participants=[{'id':row['actor_id'],'name':'用户' if row['actor_type']=='USER' else row['actor_id']}]
        if kind=='comment' and row.get('peer_id') and row['peer_id']!=character:
            participants=[{'id':row['peer_id'],'name':row['peer_id']}]
        if kind in ('group', 'comment'):
            table = 'conversation_events' if kind == 'group' else 'space_comments'
            receipt = self.store.conn.execute(f"SELECT ({collaboration_peer_sql(channel)}) FROM {table} e WHERE e.id=?", [character, row['id']]).fetchone()
            if receipt and isinstance(receipt[0], str) and receipt[0]:
                participants=[{'id':receipt[0], 'name':receipt[0]}]
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
            rows=self.store.conn.execute("SELECT id,content,active,superseded_by FROM memories WHERE character_id=? AND source_event_id=? AND CASE WHEN json_valid(metadata_json) THEN coalesce(upper(json_extract(metadata_json,'$.origin')),'') ELSE '' END<>'GROUP' ORDER BY id LIMIT 50",[character,event_id]).fetchall()
            memories=[dict(r) for r in rows]
        elif kind=='group':
            rows=self.store.conn.execute("SELECT id,content,active,superseded_by FROM memories WHERE character_id=? AND upper(json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.origin'))='GROUP' AND CAST(json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.source_conversation_event_id') AS INTEGER)=? AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.conversation_id')=? LIMIT 50",[character,row['id'],row['conversation_id']]).fetchall()
            memories=[dict(r) for r in rows]
        decision={'basis':text(trace.get('reaction')) or None,'result':None,'error':None}
        if trace:
            actions=trace.get('actions')
            if isinstance(actions,list):
                types=[text(a.get('type'),50) for a in actions if isinstance(a,dict)]
                decision['result']='、'.join(types) or '本次机会未对外表达'
                if not types or all(t in ('NO_REPLY','NO_ACTION') for t in types): status='NO_ACTION'
        reading=obj(metadata.get('rss_reading'))
        decision['execution_request_id']=metadata.get('capability_request_id') or obj(trace.get('direct_delegation')).get('request_id')
        continuation=None
        if kind=='world':
            plan=obj(row.get('plan_json')); result=obj(row.get('result_json'))
            choice=obj(plan.get('decision')).get('choice')
            status=result.get('execution_status') or row['phase']
            title='本次 World 机会未行动' if choice=='NO_ACTION' else 'World 行动机会'
            content=text(result.get('query')) or (f'已记录选择：{choice}' if choice else '决策记录尚未完成')
            decision={'basis':None,'result':text(status,80),'error':text(row.get('error')) or None}
            decision['execution_request_id']=result.get('capability_request_id')
            event_id=row.get('source_event_id')
            if plan.get('mode')=='CONTINUATION' or result.get('continuation') is True:
                title,status,content,decision,continuation=continuation_life_projection(row,plan,result)
                event_id=result.get('source_event_id')
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
        time_status="VALID"
        if kind=='intent':
            status=row['status'];content=text(row['content']);decision['basis']=text(row.get('reason')) or None
            now=datetime.now(timezone.utc)
            try:
                earliest=parse_datetime(row['earliest_at'])
                expiry=parse_datetime(row['expires_at']) if row.get('expires_at') is not None else None
                due=status=='PENDING' and earliest is not None and earliest<=now and (expiry is None or expiry>now)
                past_expiry=status=='PENDING' and expiry is not None and expiry<=now
            except (ValueError,TypeError):
                time_status="INVALID"
            if 'intent_deferral_audit' in tables:
                audit=[dict(r) for r in self.store.conn.execute("SELECT decided_at,decision,old_earliest_at,new_earliest_at,source_event_id FROM intent_deferral_audit WHERE intent_id=? ORDER BY id LIMIT 50",[row['id']])]
            if status=='EXECUTED' and any(a['decision']=='CALL_INVITATION_PROPOSED' for a in audit):
                status='CALL_INVITATION_PROPOSED'
                title='提出了通话邀请（不代表已经通话）'
                decision['result']='仅邀请已提出；是否接通以独立邀请和实际语音消息为准'
        return {'key':kind+':'+str(row.get('opportunity_id',row.get('id'))),'kind':kind,'time':row.get('event_time',row.get('created_at',row.get('started_at',row.get('decided_at')))),'title':title,'channel':channel,'status':status,'content':content,'source':{'kind':kind,'id':row.get('opportunity_id',row.get('id')),'event_id':event_id,'conversation_id':row.get('conversation_id'),'post_id':row.get('post_id') or metadata.get('post_id')},'decision':decision,'continuation':continuation,'state_change':state,'memories':memories,'participants':participants,'reading':{'scope':text(reading.get('content_scope'),80),'url':text(reading.get('url'),1000),'summary':text(metadata.get('world_summary')),'truncated':reading.get('truncated') if type(reading.get('truncated')) is bool else None,'read_characters':reading.get('read_characters') if type(reading.get('read_characters')) is int else None,'available_characters':reading.get('available_characters') if type(reading.get('available_characters')) is int else None} if reading else None,'intent':{'stored_status':row.get('status'),'earliest_at':row.get('earliest_at'),'expires_at':row.get('expires_at'),'audit':audit,'due':due,'past_expiry':past_expiry,'defer_count':row.get('deferral_count',0),'time_status':time_status} if kind=='intent' else None}
