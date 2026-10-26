"""Durable admission for bounded structured model invocations.

The existing caller owns prompts, schemas and application of the result. This
gate owns only invocation reservations and receipts, never a worker or retry loop.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from pydantic import BaseModel


class InvocationDenied(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


class ModelInvocationBudget:
    MIGRATION = 'execution/001-model-invocations'
    MAX_RESULT_BYTES = 65536

    def __init__(self, store, *, daily_max: int, clock=None):
        if type(daily_max) is not int or not 0 <= daily_max <= 2000:
            raise ValueError('invalid model invocation limit')
        self.store = store
        self.daily_max = daily_max
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        store.apply_schema_migration(self.MIGRATION, self._create_schema, immediate=True)

    def _create_schema(self):
        self.store.conn.execute(
            'CREATE TABLE IF NOT EXISTS model_invocation_receipts('
            'identity_key TEXT PRIMARY KEY,character_id TEXT NOT NULL,'
            'opportunity_id TEXT NOT NULL,step TEXT NOT NULL,request_hash TEXT NOT NULL,'
            'budget_day TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,'
            'claimed_at TEXT NOT NULL,completed_at TEXT,error_class TEXT)'
        )
        self.store.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_model_invocation_day '
            'ON model_invocation_receipts(budget_day,character_id)'
        )

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('model budget requires an aware real clock')
        return value.astimezone(timezone.utc)

    def invoke(self, *, character_id, opportunity_id, step, identity, schema, call):
        for value in (character_id, opportunity_id, step):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError('invalid model invocation identity')
        if not isinstance(identity, dict) or not identity or not callable(call):
            raise ValueError('invalid model invocation binding')
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise ValueError('invalid model result schema')
        key = _digest([character_id, opportunity_id, step])
        request_hash = _digest({'identity': identity, 'schema': schema.model_json_schema()})
        now = self._now()
        day = now.date().isoformat()
        with self.store.transaction(immediate=True):
            # The reservation must be committed before the provider starts;
            # a caller-owned transaction could otherwise roll paid work away.
            if self.store._tx_depth != 1:
                raise ValueError('model invocation cannot run inside an outer transaction')
            row = self.store.conn.execute(
                'SELECT * FROM model_invocation_receipts WHERE identity_key=?', (key,)
            ).fetchone()
            if row is not None:
                if row['request_hash'] != request_hash:
                    raise InvocationDenied('REQUEST_ID_CONFLICT')
                if row['status'] == 'SUCCESS':
                    return schema.model_validate_json(row['result_json'])
                raise InvocationDenied('PREVIOUS_FAILURE' if row['status'] == 'FAILED' else 'IN_PROGRESS_OR_UNKNOWN')
            used = self.store.conn.execute(
                'SELECT COUNT(*) FROM model_invocation_receipts WHERE character_id=? AND budget_day=?',
                (character_id, day),
            ).fetchone()[0]
            if used >= self.daily_max:
                raise InvocationDenied('BUDGET_EXHAUSTED')
            self.store.conn.execute(
                'INSERT INTO model_invocation_receipts('
                'identity_key,character_id,opportunity_id,step,request_hash,budget_day,status,claimed_at) '
                "VALUES(?,?,?,?,?,?,'STARTED',?)",
                (key, character_id, opportunity_id, step, request_hash, day, now.isoformat()),
            )
        # Only this newly admitted attempt can call the provider. In particular,
        # a timeout or process death never makes a STARTED reservation reusable.
        try:
            result = schema.model_validate(call())
            payload = _json(result.model_dump(mode='json'))
            if len(payload.encode('utf-8')) > self.MAX_RESULT_BYTES:
                raise ValueError('model invocation result exceeds receipt bound')
        except Exception as error:
            with self.store.transaction(immediate=True):
                self.store.conn.execute(
                    "UPDATE model_invocation_receipts SET status='FAILED',completed_at=?,error_class=? "
                    "WHERE identity_key=? AND status='STARTED'",
                    (self._now().isoformat(), type(error).__name__[:120], key),
                )
            raise
        # A failed receipt commit intentionally leaves STARTED/unknown and keeps
        # its budget. Do not call the provider again to reconstruct a lost result.
        with self.store.transaction(immediate=True):
            self.store.conn.execute(
                "UPDATE model_invocation_receipts SET status='SUCCESS',result_json=?,completed_at=? "
                "WHERE identity_key=? AND status='STARTED'",
                (payload, self._now().isoformat(), key),
            )
        return result


def model_budget_status(store, *, daily_max, now=None):
    """Inspect existing metadata only; GET must not create a schema or runtime."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('model budget status requires an aware clock')
    day = now.astimezone(timezone.utc).date().isoformat()
    initialized = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_invocation_receipts'"
    ).fetchone() is not None
    result = {'initialized': initialized, 'budget_day': day, 'daily_max': daily_max,
              'unit': 'structured_invocation', 'scope': 'PERSONAL_BROWSE',
              'characters': [], 'unfinished': []}
    if not initialized:
        return result
    result['characters'] = [
        {'character_id': row['character_id'], 'used': row['used'],
         'remaining': max(0, daily_max-row['used'])}
        for row in store.conn.execute(
            'SELECT character_id,COUNT(*) AS used FROM model_invocation_receipts '
            'WHERE budget_day=? GROUP BY character_id ORDER BY character_id', (day,)
        ).fetchall()
    ]
    result['unfinished'] = [dict(row) for row in store.conn.execute(
        'SELECT character_id,opportunity_id,step,budget_day,status,claimed_at '
        "FROM model_invocation_receipts WHERE status='STARTED' ORDER BY claimed_at DESC,identity_key LIMIT 30"
    ).fetchall()]
    return result


def budgeted_structured(access, model, prompt, schema, session_id, *,
                        character_id, opportunity_id, step, images=None):
    """Adapt an existing trusted World call without changing its prompt/model."""
    images = list(images or [])
    identity = {
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        'images_sha256': _digest(images),
        'model_sha256': _digest({
            'type': f'{type(model).__module__}.{type(model).__qualname__}',
            'model': str(getattr(model, 'model', '') or ''),
            'vision_model': str(getattr(model, 'vision_model', '') or '') if images else '',
            'base_url': str(getattr(model, 'base_url', '') or ''),
            'temperature': getattr(model, 'temperature', None),
            'attempts': getattr(model, 'attempts', None),
        }),
        'policy': 'personal-browse-structured-v1',
        'session_id': session_id,
    }
    def call():
        if images:
            return model.structured_with_images_for_session(prompt, images, schema, session_id)
        return model.structured_for_session(prompt, schema, session_id)
    return ModelInvocationBudget(
        access.store(), daily_max=getattr(access.settings, 'autonomous_model_daily_max', 30),
    ).invoke(character_id=character_id, opportunity_id=opportunity_id, step=step,
             identity=identity, schema=schema, call=call)
