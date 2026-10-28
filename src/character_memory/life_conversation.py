"""Validate references to existing Life facts without invoking a runtime."""
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field
from fastapi import HTTPException
from character_memory.character_life import obj, text, collaboration_peer_sql
from character_memory.time_utils import epoch_us, parse_datetime

class LifeReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    character_id: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=1, max_length=150)

def _group_participated(store, row, character):
    """Owned output, explicit collaboration receipt or bounded model input proves participation."""
    now = epoch_us(datetime.now(timezone.utc))
    member = store.conn.execute("SELECT joined_at,joined_at_epoch FROM conversation_members WHERE conversation_id=? AND actor_type='CHARACTER' AND actor_id=?", [row['conversation_id'], character]).fetchone()
    if member is None or member['joined_at_epoch'] is None or row['event_time_epoch'] is None:
        return False
    try:
        if parse_datetime(member['joined_at']) is None:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    if member['joined_at_epoch'] > row['event_time_epoch'] or row['event_time_epoch'] > now:
        return False
    if store.conn.execute(f"SELECT 1 FROM conversation_events e WHERE e.id=? AND EXISTS({collaboration_peer_sql('GROUP')})", [row['id'], character]).fetchone():
        return True
    if row['actor_type'] == 'CHARACTER' and row['actor_id'] == character:
        return True
    return bool(store.conn.execute("""
        SELECT 1 FROM (
            SELECT t.*, CASE WHEN json_valid(t.trace_json) THEN t.trace_json ELSE '{}' END AS proof
            FROM conversation_runtime_traces t
            WHERE t.character_id=? AND t.conversation_id=?
              AND t.created_at_epoch>=? AND t.created_at_epoch<=?
              AND julianday(t.created_at) IS NOT NULL
        ) t
        WHERE t.source_conversation_event_id=?
           OR EXISTS (
               SELECT 1 FROM json_each(CASE WHEN json_type(t.proof,'$.recent_group_event_ids')='array'
                    THEN json_extract(t.proof,'$.recent_group_event_ids') ELSE '[]' END) r
               WHERE r.type='integer' AND r.key>=0 AND r.key<14 AND r.value=?
           )
           OR EXISTS (
               SELECT 1 FROM json_each(CASE WHEN json_type(t.proof,'$.shared_history_retrieval.events')='array'
                    THEN json_extract(t.proof,'$.shared_history_retrieval.events') ELSE '[]' END) h
               WHERE h.key>=0 AND h.key<4 AND h.type='object'
                 AND json_extract(CASE WHEN h.type='object' THEN h.value ELSE '{}' END,'$.conversation_id')=?
                 AND json_type(CASE WHEN h.type='object' THEN h.value ELSE '{}' END,'$.source_conversation_event_id')='integer'
                 AND json_extract(CASE WHEN h.type='object' THEN h.value ELSE '{}' END,'$.source_conversation_event_id')=?
           )
        LIMIT 1
    """, [character, row['conversation_id'], row['event_time_epoch'], now,
            row['id'], row['id'], row['conversation_id'], row['id']]).fetchone())


def resolve_life_reference(store, ref):

    def fail(status, reason):
        raise HTTPException(status, reason)
    kind, _, identity = ref.key.partition(':')
    if kind not in ('events', 'group', 'post', 'comment'):
        fail(422, '这类记录暂不支持引用聊天；计划与机会不代表已发生经历。')
    if not identity.isascii() or not identity.isdigit() or len(identity) > 19 or (int(identity) > 9223372036854775807):
        fail(422, '无效来源编号')
    tables = {'events': 'events', 'group': 'conversation_events', 'post': 'space_posts', 'comment': 'space_comments'}
    with store._lock:
        existing = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables[kind] not in existing:
            fail(404, '来源记录不存在或已删除')
        raw = store.conn.execute(f'SELECT * FROM {tables[kind]} WHERE id=?', [int(identity)]).fetchone()
        if kind == 'events':
            # Chat facts may have moved to the unified Direct domain; a canonical
            # id or an imported legacy id resolves there. Non-conversation rows
            # (world readings, Space receipts) stay in the legacy table.
            direct_row = store.fact_reader.get_direct_row(ref.character_id, int(identity))
            if direct_row is not None:
                raw = direct_row
        if raw is None:
            fail(404, '来源记录不存在或已删除')
        row = dict(raw)
        meta = obj(row.get('metadata_json'))
        target = None
        if kind == 'group':
            if row['actor_type'] not in ('USER', 'CHARACTER') or row['event_type'] not in ('USER_MESSAGE', 'CHARACTER_MESSAGE'):
                fail(422, '这类群聊机会不支持引用')
            participated = _group_participated(store, row, ref.character_id)
            if not participated:
                fail(404, '来源不属于所选人物的可见经历')
            group = store.conn.execute("SELECT * FROM conversations WHERE id=? AND type='GROUP'", [row['conversation_id']]).fetchone()
            if group is None:
                fail(404, '来源群聊已删除')
            if group['archived_at']:
                fail(409, '来源群聊已归档，请先恢复')
            members = {(r['actor_type'], r['actor_id']) for r in store.conn.execute('SELECT actor_type,actor_id FROM conversation_members WHERE conversation_id=?', [row['conversation_id']])}
            if ('USER', 'user') not in members or ('CHARACTER', ref.character_id) not in members:
                fail(409, '用户或所选人物已不在来源群聊中')
            target = {'type': 'GROUP', 'id': row['conversation_id']}
        else:
            received = kind == 'comment' and store.conn.execute(f"SELECT 1 FROM space_comments e WHERE e.id=? AND EXISTS({collaboration_peer_sql('SPACE')})", [row['id'], ref.character_id]).fetchone()
            if (row.get('character_id') != ref.character_id and not received) or (kind == 'comment' and row['actor_type'] != 'CHARACTER'):
                fail(404, '来源不属于所选人物的可见经历')
            if kind in ('post', 'comment'):
                post = row if kind == 'post' else store.conn.execute('SELECT * FROM space_posts WHERE id=?', [row['post_id']]).fetchone()
                if post is None:
                    fail(404, '来源动态已删除')
                if post['visibility'] != 'ACTIVE_CHARACTERS':
                    fail(409, '来源动态当前不可见')
            if kind == 'events' and (row['event_type'] not in ('USER_MESSAGE', 'CHARACTER_MESSAGE', 'WORLD_OBSERVATION', 'SPACE_POST_SEEN', 'SPACE_COMMENT_RECEIVED', 'SOCIAL_POST') or str(meta.get('origin', '')).upper() == 'GROUP' or str(meta.get('channel', '')).upper() == 'GROUP'):
                fail(422, '这类事件暂不支持引用聊天')
            if kind == 'events' and row['event_type'] in ('USER_MESSAGE', 'CHARACTER_MESSAGE'):
                conversation = meta.get('conversation_id')
            else:
                conversation = None
            if not isinstance(conversation, str) or not conversation:
                recent = store.fact_reader.list_chat_events(ref.character_id, limit=1)
                conversation = recent[-1].metadata.get('conversation_id') if recent else None
            if not isinstance(conversation, str) or not conversation:
                fail(409, '还没有可继续的私聊，请先与该人物建立私聊')
            target = {'type': 'DIRECT', 'id': conversation}
        stamp = row.get('event_time', row.get('created_at'))
        stamp_epoch = row.get('event_time_epoch', row.get('created_at_epoch'))
        if stamp_epoch is None:
            fail(422, '来源时间无效，不能作为已发生经历引用')
        try:
            if parse_datetime(stamp) is None:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            fail(422, '来源时间无效，不能作为已发生经历引用')
        body = text(row.get('content'), 2000)
        if not body.strip():
            fail(422, '来源没有可引用的正文')
        actor = row.get('actor_id') if kind == 'group' else (row['character_id'] if kind == 'comment' else ('user' if row.get('event_type') == 'USER_MESSAGE' else ref.character_id))
        record_type = row.get('event_type', kind)
        truncated = len(row.get('content') or '') > 2000
        quote = (f'【生活记录引用：{ref.character_id} / {ref.key} / {stamp}】\n'
                 f'记录类型：{record_type}；记录发言者/观察者：{actor}。\n'
                 '以下是已有记录，不表示用户亲历；引用不是共同活动已经完成的证明。'
                 '记录正文（World 仅为人物已保存的观察，不是网页全文）：\n'
                 f'{body}\n' + ('【正文超过 2000 字符，引用已截断】\n' if truncated else '') + '【引用结束】')
        return {'read_only': True, 'reference': ref.model_dump(), 'conversation': target, 'quote': quote, 'draft': '想和你聊聊这条记录。'}

def prepare_life_message(store, ref, message, *, character_id=None, conversation_id=None):
    data = resolve_life_reference(store, ref)
    target = data['conversation']
    if character_id is not None:
        if ref.character_id != character_id or target != {'type': 'DIRECT', 'id': conversation_id}:
            raise HTTPException(409, '引用与当前私聊不匹配，请重新打开来源')
    elif target != {'type': 'GROUP', 'id': conversation_id}:
        raise HTTPException(409, '引用与当前群聊不匹配，请重新打开来源')
    return (message.strip() + '\n\n' + data['quote'], data['reference'])
