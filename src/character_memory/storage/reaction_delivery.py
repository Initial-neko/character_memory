"""Durable delivery projections referencing authoritative conversation facts."""
from dataclasses import dataclass
import json
import math
import time
from uuid import uuid4

from character_memory.domain.facts import FactRef


class DeliveryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryBatch:
    ref: FactRef
    token: str
    snapshot: dict


class ReactionDeliveryRepository:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or time.time
        def migrate():
            store.conn.execute('''CREATE TABLE reaction_delivery_sources(
                ref TEXT PRIMARY KEY, channel TEXT NOT NULL, source_id INTEGER NOT NULL,
                context_json TEXT NOT NULL, state TEXT NOT NULL, batch_ref TEXT)''')
            store.conn.execute('''CREATE TABLE reaction_delivery_batches(
                ref TEXT PRIMARY KEY, channel TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                owner_token TEXT NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
                error_kind TEXT)''')
            store.conn.execute('CREATE INDEX reaction_delivery_pending ON reaction_delivery_sources(state,channel,source_id)')
        store.apply_schema_migration('conversation/004-durable-delivery', migrate, immediate=True)

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError('delivery clock must use finite server epoch seconds')
        return now

    def record(self, ref, channel, *, character_id=None, conversation_id, media_ids=None, mentions=None):
        with self.store._lock:
            if self.store._tx_depth <= 0:
                raise RuntimeError('source and delivery must share a transaction')
            context = {'source': ref.model_dump(mode='json'), 'character_id': character_id,
                'conversation_id': conversation_id, 'media_ids': list(media_ids or []), 'mentions': list(mentions or [])}
            # A source is recorded once, by its fact owner. Do not replace its scope.
            self.store.conn.execute('INSERT INTO reaction_delivery_sources VALUES(?,?,?,?,?,NULL)',
                (ref.model_dump_json(), channel, ref.id, json.dumps(context, ensure_ascii=False), 'PENDING'))

    def pending(self, *, limit=64):
        with self.store._lock:
            rows = self.store.conn.execute('''WITH candidates AS (
                SELECT s.* FROM reaction_delivery_sources s
                LEFT JOIN reaction_delivery_batches b ON b.ref=s.batch_ref
                WHERE s.state='PENDING' AND (s.batch_ref IS NULL OR b.state='SUPERSEDED'
                    OR (b.state='RUNNING' AND b.deadline<=?)))
                SELECT c.context_json FROM candidates c JOIN
                (SELECT channel,MAX(source_id) AS latest FROM candidates GROUP BY channel) last
                ON c.channel=last.channel AND c.source_id=last.latest ORDER BY c.channel LIMIT ?''',
                (self._now(), max(1, min(256, int(limit))))).fetchall()
            return [json.loads(row[0]) for row in rows]

    def claim(self, ref, *, lease_seconds, plan_factory=None):
        duration = float(lease_seconds)
        if not math.isfinite(duration) or not 1 <= duration <= 3600:
            raise ValueError('invalid delivery lease')
        key = ref.model_dump_json()
        with self.store.transaction(immediate=True):
            now = self._now()
            source = self.store.conn.execute('SELECT * FROM reaction_delivery_sources WHERE ref=?', (key,)).fetchone()
            if source is None:
                return None  # Legacy/transient path: never invent an outbox from history.
            existing = self.store.conn.execute('SELECT * FROM reaction_delivery_batches WHERE ref=?', (key,)).fetchone()
            token = uuid4().hex
            # A superseded generation leaves its sources PENDING so the next
            # claim can fold them into a fresh batch. That fold must be
            # possible even when this ref already owns the SUPERSEDED batch,
            # otherwise the source is unclaimable forever (P15-F1).
            reclaim = (existing is not None and existing['state'] == 'SUPERSEDED'
                and source['state'] == 'PENDING'
                and source['batch_ref'] in (None, key))
            if existing is not None and not reclaim:
                if existing['state'] != 'RUNNING' or existing['deadline'] > now:
                    raise DeliveryUnavailable(existing['state'])
                payload = json.loads(existing['snapshot_json'])
                self.store.conn.execute('UPDATE reaction_delivery_batches SET owner_token=?,deadline=? WHERE ref=?',
                    (token, now + duration, key))
                return DeliveryBatch(ref, token, payload)
            if source['state'] != 'PENDING':
                raise DeliveryUnavailable(source['state'])
            rows = self.store.conn.execute('SELECT * FROM reaction_delivery_sources '
                "WHERE channel=? AND source_id<=? AND state='PENDING' ORDER BY source_id",
                (source['channel'], source['source_id'])).fetchall()
            contexts = [json.loads(row['context_json']) for row in rows]
            payload = {**json.loads(source['context_json']),
                'source_refs': [item['source'] for item in contexts],
                'media_ids': list(dict.fromkeys(value for item in contexts for value in item['media_ids'])),
                'mentions': list(dict.fromkeys(value for item in contexts for value in item['mentions']))}
            payload['plan'] = plan_factory(payload['mentions']) if plan_factory else None
            for prior in {row['batch_ref'] for row in rows if row['batch_ref']}:
                self.store.conn.execute("UPDATE reaction_delivery_batches SET state='SUPERSEDED' WHERE ref=? AND state='RUNNING'", (prior,))
            if reclaim:
                self.store.conn.execute('UPDATE reaction_delivery_batches SET channel=?,snapshot_json=?,owner_token=?,'
                    "deadline=?,state='RUNNING',error_kind=NULL WHERE ref=?",
                    (source['channel'], json.dumps(payload, ensure_ascii=False), token, now + duration, key))
            else:
                self.store.conn.execute('INSERT INTO reaction_delivery_batches VALUES(?,?,?,?,?,?,NULL)',
                    (key, source['channel'], json.dumps(payload, ensure_ascii=False), token, now + duration, 'RUNNING'))
            self.store.conn.executemany('UPDATE reaction_delivery_sources SET batch_ref=? WHERE ref=?',
                [(key, row['ref']) for row in rows])
            return DeliveryBatch(ref, token, payload)

    def owns(self, batch):
        with self.store._lock:
            return self.store.conn.execute("SELECT 1 FROM reaction_delivery_batches WHERE ref=? "
                "AND owner_token=? AND state='RUNNING'", (batch.ref.model_dump_json(), batch.token)).fetchone() is not None

    def finish(self, batch, state, *, error_kind=None):
        if state not in {'COGNITION_COMMITTED', 'SUPERSEDED', 'UNKNOWN', 'BLOCKED'}:
            raise ValueError('invalid delivery terminal state')
        with self.store.transaction(immediate=True):
            changed = self.store.conn.execute('UPDATE reaction_delivery_batches SET state=?,error_kind=? '
                "WHERE ref=? AND owner_token=? AND state='RUNNING'",
                (state, error_kind, batch.ref.model_dump_json(), batch.token)).rowcount
            if changed and state != 'SUPERSEDED':
                self.store.conn.execute("UPDATE reaction_delivery_sources SET state=? WHERE batch_ref=? AND state='PENDING'",
                    (state, batch.ref.model_dump_json()))
            return changed == 1

    def status(self, ref):
        with self.store._lock:
            row = self.store.conn.execute('SELECT s.state,b.state AS batch_state FROM reaction_delivery_sources s '
                'LEFT JOIN reaction_delivery_batches b ON b.ref=s.batch_ref WHERE s.ref=?', (ref.model_dump_json(),)).fetchone()
            return (row['batch_state'] or row['state']) if row else 'NOT_TRACKED'

    def discard_stale(self, ref):
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute('SELECT channel,source_id FROM reaction_delivery_sources WHERE ref=?',
                (ref.model_dump_json(),)).fetchone()
            if row is None:
                return
            old = self.store.conn.execute("SELECT DISTINCT batch_ref FROM reaction_delivery_sources "
                "WHERE channel=? AND source_id<=? AND state='PENDING'", (row['channel'], row['source_id'])).fetchall()
            for batch in old:
                self.store.conn.execute("UPDATE reaction_delivery_batches SET state='SUPERSEDED' WHERE ref=? AND state='RUNNING'", (batch[0],))
            self.store.conn.execute("UPDATE reaction_delivery_sources SET state='SUPERSEDED' "
                "WHERE channel=? AND source_id<=? AND state='PENDING'", (row['channel'], row['source_id']))

    def defer(self, batch, until):
        with self.store.transaction(immediate=True):
            self.store.conn.execute("UPDATE reaction_delivery_batches SET deadline=? "
                "WHERE ref=? AND owner_token=? AND state='RUNNING'",
                (max(self._now(), float(until)), batch.ref.model_dump_json(), batch.token))

    def block(self, ref, *, reason):
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute("SELECT batch_ref FROM reaction_delivery_sources WHERE ref=? AND state='PENDING'",
                (ref.model_dump_json(),)).fetchone()
            if row is not None:
                self.store.conn.execute("UPDATE reaction_delivery_sources SET state='BLOCKED' WHERE ref=?", (ref.model_dump_json(),))
                if row['batch_ref']:
                    self.store.conn.execute("UPDATE reaction_delivery_batches SET state='BLOCKED',error_kind=? "
                        "WHERE ref=? AND state IN ('RUNNING','SUPERSEDED')", (reason, row['batch_ref']))
