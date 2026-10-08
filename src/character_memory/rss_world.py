"""Character-local receipts for bounded reading of already-collected RSS text.

Collection remains owned by RssRepository. Nothing here fetches a URL, invokes
an LLM, changes a shared item's read status, or creates character cognition.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json


class RssPersonalReading:
    MIGRATION = 'world/006-rss-personal-reading'

    def __init__(self, store):
        self.store = store
        self.store.apply_schema_migration(self.MIGRATION, self._create_schema, immediate=True)

    def _create_schema(self):
        self.store.conn.execute(
            'CREATE TABLE IF NOT EXISTS world_rss_readings('
            'character_id TEXT NOT NULL,item_id INTEGER NOT NULL,request_id TEXT NOT NULL,'
            'status TEXT NOT NULL,attempts INTEGER NOT NULL,read_at TEXT NOT NULL,'
            'snapshot_json TEXT NOT NULL,source_event_id INTEGER,'
            'PRIMARY KEY(character_id,item_id))'
        )

    @staticmethod
    def _hash(row) -> str:
        # Fetch/error timestamps do not change the content version or wake idle
        # characters. Full stored plain text is hashed, not its preview.
        value = [row['title'], row['url'], row['summary'], row['content_text']]
        return sha256(json.dumps(value, ensure_ascii=False).encode('utf-8')).hexdigest()

    def candidates(self, character_id: str, *, limit: int = 8) -> list[dict]:
        limit = max(1, min(8, int(limit)))
        with self.store._lock:
            rows = self.store.conn.execute(
                'SELECT i.*,s.name AS source_name,s.subscription_generation FROM rss_items i '
                'JOIN rss_sources s ON s.id=i.source_id '
                'LEFT JOIN world_rss_readings r ON r.item_id=i.id AND r.character_id=? '
                "WHERE s.enabled=1 AND s.cancelled_at IS NULL AND trim(i.content_text)<>'' "
                "AND (r.item_id IS NULL OR (r.status='FAILED' AND r.attempts<2)) "
                'ORDER BY COALESCE(i.published_at_epoch,i.fetched_at_epoch) DESC,i.id DESC LIMIT ?',
                (character_id, limit),
            ).fetchall()
        return [{'item_id': row['id'], 'source_id': row['source_id'],
                 'source_generation': row['subscription_generation'],
                 'title': row['title'][:240], 'summary': row['summary'][:400],
                 'source_name': row['source_name'][:200], 'url': row['url'][:2048],
                 'published_at': row['published_at'], 'content_hash': self._hash(row)} for row in rows]

    def signal(self, character_id: str, *, limit: int = 8) -> str:
        values = [(item['item_id'], item['source_generation'], item['content_hash'])
                  for item in self.candidates(character_id, limit=limit)]
        return sha256(json.dumps(values).encode()).hexdigest()

    def read(self, character_id: str, request_id: str, selected: list[dict], *,
             max_items: int, max_chars: int, now: datetime | None = None) -> dict:
        if max_items not in (1, 2) or not 500 <= max_chars <= 12000:
            raise ValueError('invalid local RSS read bounds')
        if not selected or len(selected) > max_items:
            raise ValueError('invalid local RSS selection')
        identities = [item['item_id'] for item in selected]
        if any(type(item) is not int or item < 1 for item in identities) or len(set(identities)) != len(identities):
            raise ValueError('invalid local RSS item identity')
        now = now or datetime.now().astimezone()
        result = {'items': [], 'errors': []}
        # Source state, snapshot and per-person claim share a transaction. A
        # second opportunity cannot acquire an item already STARTED elsewhere.
        with self.store.transaction(immediate=True):
            for selected_item in selected:
                item_id = selected_item['item_id']
                row = self.store.conn.execute(
                    'SELECT i.*,s.name AS source_name,s.subscription_generation,s.feed_url '
                    'FROM rss_items i JOIN rss_sources s ON s.id=i.source_id '
                    'WHERE i.id=? AND s.enabled=1 AND s.cancelled_at IS NULL '
                    'AND s.subscription_generation=?',
                    (item_id, selected_item['source_generation']),
                ).fetchone()
                existing = self.store.conn.execute(
                    'SELECT * FROM world_rss_readings WHERE character_id=? AND item_id=?',
                    (character_id, item_id),
                ).fetchone()
                if row is None:
                    result['errors'].append({'item_id': item_id, 'reason': 'SOURCE_CHANGED_OR_UNAVAILABLE'})
                    continue
                if existing is not None and (existing['status'] != 'FAILED' or existing['attempts'] >= 2):
                    result['errors'].append({'item_id': item_id, 'reason': 'ALREADY_HANDLED_OR_UNKNOWN'})
                    continue
                content = row['content_text'].strip()[:max_chars // len(selected)]
                snapshot = {'item_id': item_id, 'source_id': row['source_id'],
                            'source_generation': row['subscription_generation'],
                            'title': row['title'][:240], 'url': row['url'][:2048],
                            'source_name': row['source_name'][:200], 'feed_url': row['feed_url'][:2048],
                            'published_at': row['published_at'], 'read_at': now.isoformat(),
                            'content_scope': 'RSS_FEED_TEXT', 'content': content,
                            'content_hash': self._hash(row), 'read_characters': len(content),
                            'available_characters': len(row['content_text'].strip()),
                            'truncated': len(row['content_text'].strip()) > len(content)}
                status = 'STARTED' if content else 'FAILED'
                attempts = existing['attempts'] + 1 if existing else 1
                self.store.conn.execute(
                    'INSERT INTO world_rss_readings(character_id,item_id,request_id,status,attempts,read_at,snapshot_json) '
                    'VALUES(?,?,?,?,?,?,?) ON CONFLICT(character_id,item_id) DO UPDATE SET '
                    'request_id=excluded.request_id,status=excluded.status,attempts=excluded.attempts,'
                    'read_at=excluded.read_at,snapshot_json=excluded.snapshot_json,source_event_id=NULL',
                    (character_id, item_id, request_id, status, attempts, now.isoformat(),
                     json.dumps(snapshot, ensure_ascii=False)),
                )
                if content:
                    result['items'].append(snapshot)
                else:
                    result['errors'].append({'item_id': item_id, 'reason': 'NO_LOCAL_CONTENT'})
        return result

    def finish(self, character_id: str, request_id: str, item_id: int, *,
               status: str, source_event_id: int | None = None) -> bool:
        if status not in {'APPLIED', 'IGNORED', 'FAILED'}:
            raise ValueError('invalid RSS reading outcome')
        with self.store.transaction(immediate=True):
            cursor = self.store.conn.execute(
                "UPDATE world_rss_readings SET status=?,source_event_id=? WHERE character_id=? "
                "AND item_id=? AND request_id=? AND status='STARTED'",
                (status, source_event_id, character_id, item_id, request_id),
            )
            return cursor.rowcount > 0
