"""Bounded in-app Direct invitations. Acceptance is never a completed call fact."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi import HTTPException
from character_memory.domain.models import EventType
from character_memory.time_utils import epoch_us
SESSION_LEASE_SECONDS = 90

@dataclass(frozen=True)
class CallInvitationPolicy:
    enabled: bool = False
    ttl_seconds: int = 120
    cooldown_minutes: float = 60

    @classmethod
    def from_settings(cls, settings):
        return cls(bool(getattr(settings, 'direct_call_invitations_enabled', False)), int(getattr(settings, 'direct_call_invitation_ttl_seconds', 120)), float(getattr(settings, 'proactive_min_dispatch_interval_minutes', 60)))

def direct_call_source(event):
    meta = event.metadata
    return event.event_type in (EventType.USER_MESSAGE, EventType.TIME_TICK, EventType.PROACTIVE_INTENT) and meta.get('channel') == 'DIRECT' and bool(meta.get('conversation_id')) and (meta.get('source_conversation_event_id') is None) and (meta.get('group_event_id') is None) and (str(meta.get('origin', '')).upper() not in ('GROUP', 'SPACE', 'WORLD'))

class CallInvitationRepository:

    def __init__(self, store):
        self.store = store

        def migrate():
            store.conn.execute("CREATE TABLE IF NOT EXISTS call_invitations(\n                id TEXT PRIMARY KEY, character_id TEXT NOT NULL, conversation_id TEXT NOT NULL,\n                source_event_id INTEGER NOT NULL UNIQUE, message TEXT NOT NULL, reason TEXT NOT NULL,\n                status TEXT NOT NULL CHECK(status IN ('PENDING','ACCEPTED','DECLINED','EXPIRED','CANCELLED')),\n                created_at TEXT NOT NULL, created_at_epoch INTEGER NOT NULL,\n                expires_at TEXT NOT NULL, expires_at_epoch INTEGER NOT NULL,\n                accepted_at TEXT, connected_at TEXT, ended_at TEXT, end_reason TEXT NOT NULL DEFAULT '',\n                client_id TEXT, session_token TEXT, lease_until_epoch INTEGER, updated_at TEXT NOT NULL)")
            store.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_call_invitation_active ON call_invitations(character_id) WHERE status IN ('PENDING','ACCEPTED')")
            store.conn.execute('CREATE INDEX IF NOT EXISTS idx_call_invitation_created ON call_invitations(character_id,created_at_epoch DESC)')
        store.apply_schema_migration('calls/001-direct-invitations', migrate)

    def _expire(self, now):
        stamp = epoch_us(now)
        iso = now.isoformat()
        self.store.conn.execute("UPDATE call_invitations SET status='EXPIRED',updated_at=?,ended_at=?,end_reason='NOT_ACCEPTED' WHERE status='PENDING' AND expires_at_epoch<=?", [iso, iso, stamp])
        self.store.conn.execute("UPDATE call_invitations SET status='CANCELLED',updated_at=?,ended_at=?,end_reason='DISCONNECTED' WHERE status='ACCEPTED' AND lease_until_epoch<=?", [iso, iso, stamp])

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('id', 'character_id', 'conversation_id', 'source_event_id', 'message', 'reason', 'status', 'created_at', 'expires_at', 'accepted_at', 'connected_at', 'ended_at', 'end_reason', 'updated_at')}

    def can_offer(self, event, policy):
        if not policy.enabled or not direct_call_source(event) or event.id is None:
            return False
        with self.store._lock:
            active = self.store.conn.execute("SELECT 1 FROM call_invitations WHERE character_id=? AND (status='PENDING' AND expires_at_epoch>? OR status='ACCEPTED' AND lease_until_epoch>?)", [event.character_id, epoch_us(event.event_time), epoch_us(event.event_time)]).fetchone()
            recent = self.store.conn.execute('SELECT 1 FROM call_invitations WHERE character_id=? AND created_at_epoch>?', [event.character_id, epoch_us(event.event_time - timedelta(minutes=max(1, policy.cooldown_minutes)))]).fetchone()
            return not active and (not recent)

    def offer(self, event, message, reason, policy):
        if not policy.enabled or not direct_call_source(event):
            return None
        with self.store.transaction(immediate=True):
            self._expire(event.event_time)
            if not self.can_offer(event, policy):
                return None
            source = self.store.get_event(event.id)
            if source is None or source.character_id != event.character_id or (not direct_call_source(source)):
                return None
            identifier = str(uuid4())
            now = event.event_time
            expires = now + timedelta(seconds=policy.ttl_seconds)
            self.store.conn.execute("INSERT OR IGNORE INTO call_invitations(id,character_id,conversation_id,source_event_id,message,reason,status,created_at,created_at_epoch,expires_at,expires_at_epoch,updated_at) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?,?)", [identifier, event.character_id, event.metadata['conversation_id'], event.id, str(message).strip()[:500], str(reason).strip()[:200], now.isoformat(), epoch_us(now), expires.isoformat(), epoch_us(expires), now.isoformat()])
            row = self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone()
            return self._public(row) if row else None

    def list(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self.store.transaction(immediate=True):
            self._expire(now)
            return [self._public(r) for r in self.store.conn.execute("SELECT * FROM call_invitations WHERE status IN ('PENDING','ACCEPTED') ORDER BY created_at_epoch DESC,id DESC LIMIT 50")]

    def get(self, identifier, *, now=None):
        with self.store.transaction(immediate=True):
            self._expire(now or datetime.now(timezone.utc))
            row = self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone()
            if row is None:
                raise HTTPException(404, '来电邀请不存在')
            return self._public(row)

    def transition(self, identifier, operation, *, now=None, client_id='', session_token=''):
        now = now or datetime.now(timezone.utc)
        with self.store.transaction(immediate=True):
            # Expire inside the same write transaction. A separate read before
            # it let an invite that expired while waiting for the lock be
            # accepted with a session token that was already invalid.
            self._expire(now)
            row = self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone()
            if row is None:
                raise HTTPException(404, '来电邀请不存在')
            target = {'accept': 'ACCEPTED', 'decline': 'DECLINED', 'cancel': 'CANCELLED'}[operation]
            if row['status'] == target:
                if target == 'ACCEPTED' and row['client_id'] != client_id:
                    raise HTTPException(409, '邀请已被另一页面接受')
                return {'changed': False, 'item': self._public(row), **({'session_token': row['session_token']} if target == 'ACCEPTED' else {})}
            if row['status'] not in ('PENDING', 'ACCEPTED') or (row['status'] == 'ACCEPTED' and operation != 'cancel'):
                raise HTTPException(409, '来电邀请已处理或已过期')
            if row['status'] == 'ACCEPTED' and session_token != row['session_token']:
                raise HTTPException(409, '通话会话已变更')
            token = str(uuid4()) if operation == 'accept' else None
            lease = epoch_us(now + timedelta(seconds=SESSION_LEASE_SECONDS)) if token else None
            self.store.conn.execute('UPDATE call_invitations SET status=?,accepted_at=?,ended_at=?,end_reason=?,client_id=?,session_token=?,lease_until_epoch=?,updated_at=? WHERE id=?', [target, now.isoformat() if token else row['accepted_at'], None if token else now.isoformat(), 'USER_DECLINED' if operation == 'decline' else 'USER_CANCELLED' if operation == 'cancel' else '', client_id or row['client_id'], token or row['session_token'], lease, now.isoformat(), identifier])
            row = self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone()
            return {'changed': True, 'item': self._public(row), **({'session_token': token} if token else {})}

    def lease(self, identifier, session_token, *, now=None, connected=False):
        now = now or datetime.now(timezone.utc)
        self.get(identifier, now=now)
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone()
            if row['status'] != 'ACCEPTED' or session_token != row['session_token']:
                raise HTTPException(409, '来电会话已结束，不能重新接通')
            self.store.conn.execute('UPDATE call_invitations SET lease_until_epoch=?,connected_at=COALESCE(connected_at,?),updated_at=? WHERE id=?', [epoch_us(now + timedelta(seconds=SESSION_LEASE_SECONDS)), now.isoformat() if connected else None, now.isoformat(), identifier])
            return self._public(self.store.conn.execute('SELECT * FROM call_invitations WHERE id=?', [identifier]).fetchone())

    def cancel_open(self, *, character_id=None, reason='PERMISSION_DISABLED', now=None):
        now = now or datetime.now(timezone.utc)
        with self.store.transaction(immediate=True):
            sql = "UPDATE call_invitations SET status='CANCELLED',ended_at=?,updated_at=?,end_reason=? WHERE status IN ('PENDING','ACCEPTED')"
            values = [now.isoformat(), now.isoformat(), reason]
            if character_id is not None:
                sql += ' AND character_id=?'
                values.append(character_id)
            self.store.conn.execute(sql, values)
