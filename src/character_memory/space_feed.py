"""Read-only, typed Space projection over existing durable content owners."""
from __future__ import annotations

import base64
import json
from urllib.parse import quote, urlsplit


def source_url(value: str) -> str:
    """Navigation only; this does not fetch or certify a source as reliable."""
    try:
        parsed = urlsplit(str(value or '').strip())
        if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
            return parsed.geturl()
    except ValueError:
        pass
    return ''


class SpaceFeed:
    def __init__(self, store):
        self.store = store

    def page(self, *, kind='ALL', limit=10, cursor=None, source_id=None, character_id=None,
             character_ids=None):
        if kind not in {'ALL', 'POST', 'RSS', 'TOPIC'}:
            raise ValueError('invalid Space content kind')
        limit = max(1, min(30, int(limit)))
        after = None
        if cursor:
            try:
                if len(cursor) > 200:
                    raise ValueError()
                after = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
                if (not isinstance(after, list) or len(after) != 3
                        or type(after[0]) is not int or type(after[2]) is not int
                        or not -(2**63) <= after[0] < 2**63 or not 1 <= after[2] < 2**63
                        or after[1] not in {'POST', 'RSS', 'TOPIC'}):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError('invalid Space cursor') from exc
        with self.store._lock:
            tables = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            selects, args = [], []
            if 'space_posts' in tables and kind in {'ALL', 'POST'} and source_id is None:
                authors = None if character_ids is None else [str(value) for value in character_ids]
                if authors != []:
                    clause = "visibility='ACTIVE_CHARACTERS'"
                    if authors:
                        clause += ' AND character_id IN (' + ','.join('?' * len(authors)) + ')'
                        args.extend(authors)
                    if character_id:
                        clause += ' AND character_id=?'
                        args.append(character_id)
                    selects.append(f"SELECT created_at_epoch AS stamp,'POST' AS kind,id FROM space_posts WHERE {clause}")
            if not character_id and kind in {'ALL', 'RSS'} and {'rss_items', 'rss_sources'} <= tables:
                clause = 's.cancelled_at IS NULL'
                if source_id is not None:
                    clause += ' AND i.source_id=?'
                    args.append(int(source_id))
                selects.append("SELECT COALESCE(i.published_at_epoch,i.fetched_at_epoch) AS stamp,'RSS' AS kind,i.id "
                               f"FROM rss_items i JOIN rss_sources s ON s.id=i.source_id WHERE {clause}")
            if not character_id and source_id is None and kind in {'ALL', 'TOPIC'} and 'world_pulse_topics' in tables:
                # first_seen is immutable; updating a topic cannot move it across pages.
                selects.append("SELECT first_seen_at_epoch AS stamp,'TOPIC' AS kind,id FROM world_pulse_topics")
            if not selects:
                return {'items': [], 'has_more': False, 'next_cursor': None}
            sql = 'SELECT * FROM (' + ' UNION ALL '.join(selects) + ')'
            if after:
                sql += ' WHERE (stamp,kind,id) < (?,?,?)'
                args.extend(after)
            sql += ' ORDER BY stamp DESC,kind DESC,id DESC LIMIT ?'
            args.append(limit + 1)
            rows = self.store.conn.execute(sql, args).fetchall()
            items = [self._project(row) for row in rows[:limit]]
        has_more = len(rows) > limit
        last = rows[limit - 1] if has_more else None
        next_cursor = (base64.urlsafe_b64encode(json.dumps([last['stamp'], last['kind'], last['id']]).encode()).decode()
                       if last else None)
        return {'items': items, 'has_more': has_more, 'next_cursor': next_cursor}

    def _project(self, row):
        kind, identity = row['kind'], int(row['id'])
        base = {'key': f'{kind.lower()}:{identity}', 'kind': kind, 'id': identity}
        if kind == 'POST':
            return base  # The Space route reuses its full, existing post projection.
        if kind == 'RSS':
            item = self.store.conn.execute(
                'SELECT i.*,s.name AS source_name,s.feed_url FROM rss_items i '
                'JOIN rss_sources s ON s.id=i.source_id WHERE i.id=?', (identity,),
            ).fetchone()
            return {**base, 'title': item['title'], 'summary': item['summary'][:700],
                    'created_at': item['published_at'] or item['fetched_at'],
                    'published_at': item['published_at'], 'fetched_at': item['fetched_at'],
                    'content_scope': 'RSS_FEED_TEXT',
                    'verification': 'SOURCE_CONTENT_NOT_INDEPENDENTLY_VERIFIED',
                    'source': {'id': item['source_id'], 'name': item['source_name'],
                               'url': source_url(item['url']), 'feed_url': source_url(item['feed_url'])},
                    'image_url': (f'/v1/rss/items/{identity}/image?url=' + quote(item['image_url'], safe='')
                                  if source_url(item['image_url']) else '')}
        topic = self.store.conn.execute('SELECT * FROM world_pulse_topics WHERE id=?', (identity,)).fetchone()
        urls = json.loads(topic['source_urls_json'] or '[]')
        comments = self.store.conn.execute(
            'SELECT id,character_id,content,created_at FROM world_pulse_comments WHERE topic_id=? ORDER BY created_at_epoch,id',
            (identity,),
        ).fetchall()
        return {**base, 'title': topic['title'], 'summary': topic['summary'],
                'category': topic['category'], 'created_at': topic['first_seen_at'], 'updated_at': topic['updated_at'],
                'source_urls': list(dict.fromkeys(url for raw in urls if (url := source_url(raw)))),
                'verification': 'UNVERIFIED_AGGREGATION',
                'evidence': json.loads(topic['evidence_json'] or '[]') if 'evidence_json' in topic.keys() else [],
                'comments': [dict(comment) for comment in comments]}
