from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import threading
import secrets
import hmac
import uuid
from typing import Any

from character_memory.time_utils import epoch_us, parse_datetime


ENCOUNTER_SOURCE_TYPES = {"WEB", "GENERATED", "CLASSIC"}
ENCOUNTER_STATUSES = {"NEW", "SEEN", "CHATTING", "ACCEPTED", "DISMISSED", "EXPIRED", "FAILED"}


class EncounterRepository:
    """Durable candidate pool kept separate from formal character personas."""

    def __init__(self, store):
        self.store = store
        # EncounterService is instantiated both for HTTP routes and the
        # scheduler. Keep lifecycle serialization on their shared repository so
        # those callers cannot accept/dismiss/reopen one candidate concurrently.
        # A stable lock per durable candidate prevents an unrelated candidate
        # from blocking on a slow character creator due to modulo collisions.
        # Keep entries strongly owned: callers hold a raw RLock and eviction
        # without tracking waiters could hand out two locks for one candidate.
        self._lifecycle_guard = threading.Lock()
        self._lifecycle_locks: dict[int, threading.RLock] = {}
        self._init_schema()

    def lifecycle_lock(self, candidate_id: int) -> threading.RLock:
        key = int(candidate_id)
        with self._lifecycle_guard:
            lock = self._lifecycle_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._lifecycle_locks[key] = lock
            return lock

    def _init_schema(self) -> None:
        now = datetime.now().astimezone()
        with self.store._lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS encounter_candidates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    encounter_hook TEXT NOT NULL DEFAULT '',
                    opening_message TEXT NOT NULL DEFAULT '',
                    source_query TEXT NOT NULL DEFAULT '',
                    source_urls_json TEXT NOT NULL DEFAULT '[]',
                    source_domains_json TEXT NOT NULL DEFAULT '[]',
                    fingerprint TEXT NOT NULL,
                    accepted_character_id TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_encounter_candidates_fingerprint
                    ON encounter_candidates(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_encounter_candidates_status_created
                    ON encounter_candidates(status,created_at_epoch DESC,id DESC);

                CREATE TABLE IF NOT EXISTS encounter_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_encounter_messages_candidate
                    ON encounter_messages(candidate_id,created_at_epoch,id);

                CREATE TABLE IF NOT EXISTS encounter_state(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    last_opportunity_at TEXT,
                    last_opportunity_at_epoch INTEGER,
                    next_opportunity_at TEXT NOT NULL,
                    next_opportunity_at_epoch INTEGER NOT NULL,
                    last_status TEXT,
                    last_candidate_id INTEGER,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS encounter_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheduled_for TEXT NOT NULL,
                    scheduled_for_epoch INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    started_at_epoch INTEGER NOT NULL,
                    completed_at TEXT,
                    completed_at_epoch INTEGER,
                    source_type TEXT,
                    status TEXT NOT NULL,
                    candidate_id INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_encounter_runs_started
                    ON encounter_runs(started_at_epoch DESC,id DESC);
                """
            )
            self.store._ensure_migration_table_locked()
            columns = {row[1] for row in self.store.conn.execute('PRAGMA table_info(encounter_candidates)')}
            for name, declaration in (
                ('encounter_kind', "TEXT NOT NULL DEFAULT 'ORIGINAL'"),
                ('daily_key', 'TEXT'),
                ('classic_key', "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    self.store.conn.execute(f'ALTER TABLE encounter_candidates ADD COLUMN {name} {declaration}')
            self.store.conn.executescript('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_encounter_daily_key ON encounter_candidates(daily_key);
                CREATE TABLE IF NOT EXISTS encounter_daily_slots(
                    slot_key TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, candidate_id INTEGER, error TEXT NOT NULL DEFAULT '',
                    updated_epoch INTEGER NOT NULL, retry_epoch INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS encounter_calls(
                    id TEXT PRIMARY KEY, candidate_id INTEGER NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', token TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL, connected_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_encounter_calls_candidate ON encounter_calls(candidate_id);
            ''')
            self.store.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)",
                ("encounter/001-core", now.isoformat()),
            )
            self.store.conn.execute('INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)',
                ('encounter/002-daily-calls',now.isoformat()))
            self.store._maybe_commit()

    @staticmethod
    def _loads(raw: str, default):
        try:
            value = json.loads(raw or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value

    def _candidate(self, row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "source_type": str(row["source_type"]),
            "encounter_kind": str(row['encounter_kind']),
            "daily_key": row['daily_key'],
            "classic_key": str(row['classic_key']),
            "status": str(row["status"]),
            "draft": self._loads(row["draft_json"], {}),
            "encounter_hook": str(row["encounter_hook"] or ""),
            "opening_message": str(row["opening_message"] or ""),
            "source_query": str(row["source_query"] or ""),
            "source_urls": self._loads(row["source_urls_json"], []),
            "source_domains": self._loads(row["source_domains_json"], []),
            "accepted_character_id": row["accepted_character_id"],
            "error": str(row["error"] or ""),
            "created_at": str(row["created_at"]),
            'encounter_day': parse_datetime(str(row['created_at'])).astimezone().date().isoformat(),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def fingerprint(draft: dict[str, Any], source_type: str, source_urls: list[str]) -> str:
        identity = " ".join(
            str(draft.get(key) or "").strip().casefold()
            for key in ("name", "identity", "tagline", "description")
        )
        material = f"{source_type}|{identity}|{'|'.join(source_urls[:3])}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def create_candidate(
        self,
        *,
        source_type: str,
        draft: dict[str, Any],
        encounter_hook: str,
        opening_message: str,
        now: datetime,
        source_query: str = "",
        source_urls: list[str] | None = None,
        source_domains: list[str] | None = None,
        encounter_kind: str = 'ORIGINAL',
        daily_key: str | None = None,
        classic_key: str = '',
    ) -> dict[str, Any]:
        source = str(source_type or "").strip().upper()
        if source not in ENCOUNTER_SOURCE_TYPES:
            raise ValueError("invalid encounter source type")
        urls = [str(item).strip() for item in (source_urls or []) if str(item).strip()][:8]
        domains = [str(item).strip() for item in (source_domains or []) if str(item).strip()][:8]
        fingerprint = self.fingerprint(draft, source, urls)
        if daily_key:
            fingerprint = hashlib.sha256(f'{fingerprint}:{daily_key}'.encode()).hexdigest()
        stamp = epoch_us(now)
        with self.store._lock:
            existing = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE fingerprint=? OR (? IS NOT NULL AND daily_key=?)",
                (fingerprint, daily_key, daily_key),
            ).fetchone()
            if existing is not None:
                return self._candidate(existing)
            cur = self.store.conn.execute(
                """
                INSERT INTO encounter_candidates(
                    source_type,status,draft_json,encounter_hook,opening_message,
                    source_query,source_urls_json,source_domains_json,fingerprint,
                    created_at,created_at_epoch,updated_at,updated_at_epoch,encounter_kind,daily_key,classic_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source,
                    "NEW",
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    str(encounter_hook or "").strip()[:1200],
                    str(opening_message or "").strip()[:1200],
                    str(source_query or "").strip()[:500],
                    json.dumps(urls, ensure_ascii=False),
                    json.dumps(domains, ensure_ascii=False),
                    fingerprint,
                    now.isoformat(),
                    stamp,
                    now.isoformat(),
                    stamp,
                    encounter_kind,
                    daily_key,
                    classic_key,
                ),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE id=?",
                (int(cur.lastrowid),),
            ).fetchone()
        return self._candidate(row)

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM encounter_candidates WHERE id=?",
                (int(candidate_id),),
            ).fetchone()
        return self._candidate(row) if row is not None else None

    def list_candidates(self, *, limit: int = 20, include_closed: bool = True, view: str = 'all', offset: int = 0, now: datetime | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM encounter_candidates"
        args: list[Any] = []
        conditions=[]
        if view=='ongoing':
            conditions.append("status IN ('SEEN','CHATTING')")
        elif view=='kept':
            conditions.append("status='ACCEPTED'")
        elif view=='today':
            current=now or datetime.now().astimezone()
            start=current.replace(hour=0,minute=0,second=0,microsecond=0)
            conditions.extend(["status IN ('NEW','SEEN','CHATTING')",'created_at_epoch>=? AND created_at_epoch<?'])
            args.extend([epoch_us(start),epoch_us(start+timedelta(days=1))])
        elif not include_closed:
            conditions.append("status IN ('NEW','SEEN','CHATTING')")
        if conditions:sql+=' WHERE '+' AND '.join(conditions)
        sql += " ORDER BY created_at_epoch DESC,id DESC LIMIT ? OFFSET ?"
        args.extend([max(1,min(int(limit),100)),max(0,int(offset))])
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._candidate(row) for row in rows]

    def pending_count(self) -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM encounter_candidates WHERE status IN ('NEW','SEEN','CHATTING')"
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def set_status(
        self,
        candidate_id: int,
        status: str,
        now: datetime,
        *,
        accepted_character_id: str | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        normalized = str(status or "").strip().upper()
        if normalized not in ENCOUNTER_STATUSES:
            raise ValueError("invalid encounter status")
        with self.store._lock:
            self.store.conn.execute(
                """
                UPDATE encounter_candidates
                SET status=?,accepted_character_id=COALESCE(?,accepted_character_id),
                    error=?,updated_at=?,updated_at_epoch=?
                WHERE id=?
                """,
                (
                    normalized,
                    accepted_character_id,
                    str(error or "")[:2000],
                    now.isoformat(),
                    epoch_us(now),
                    int(candidate_id),
                ),
            )
            if normalized in {'ACCEPTED','DISMISSED','EXPIRED','FAILED'}:
                self.store.conn.execute("UPDATE encounter_calls SET status='CANCELLED',updated_at=? WHERE candidate_id=? AND status IN ('RINGING','ACCEPTED','CONNECTED')",(now.isoformat(),candidate_id))
            self.store._maybe_commit()
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError("encounter candidate not found")
        return candidate

    def append_message(self, candidate_id: int, role: str, content: str, now: datetime) -> dict[str, Any]:
        if self.get_candidate(candidate_id) is None:
            raise KeyError("encounter candidate not found")
        normalized_role = str(role or "").strip().upper()
        if normalized_role not in {"USER", "CHARACTER"}:
            raise ValueError("invalid encounter message role")
        text = str(content or "").strip()
        if not text:
            raise ValueError("encounter message must not be empty")
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO encounter_messages(candidate_id,role,content,created_at,created_at_epoch) VALUES(?,?,?,?,?)",
                (int(candidate_id), normalized_role, text[:8000], now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
        return {
            "id": int(cur.lastrowid),
            "candidate_id": int(candidate_id),
            "role": normalized_role,
            "content": text[:8000],
            "created_at": now.isoformat(),
        }

    def append_open_message(
        self,
        candidate_id: int,
        role: str,
        content: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Append and mark chatting only if the encounter is still open.

        The status check, message insert, and transition share one transaction.
        A model result that finishes after accept/dismiss/expiry therefore
        cannot write a late line or reopen the candidate.
        """

        normalized_role = str(role or "").strip().upper()
        if normalized_role not in {"USER", "CHARACTER"}:
            raise ValueError("invalid encounter message role")
        text = str(content or "").strip()
        if not text:
            raise ValueError("encounter message must not be empty")
        with self.store.transaction():
            candidate = self.store.conn.execute(
                "SELECT status FROM encounter_candidates WHERE id=?",
                (int(candidate_id),),
            ).fetchone()
            if candidate is None:
                raise KeyError("encounter candidate not found")
            if str(candidate["status"]) in {"ACCEPTED", "DISMISSED", "EXPIRED", "FAILED"}:
                return None
            cur = self.store.conn.execute(
                "INSERT INTO encounter_messages(candidate_id,role,content,created_at,created_at_epoch) VALUES(?,?,?,?,?)",
                (int(candidate_id), normalized_role, text[:8000], now.isoformat(), epoch_us(now)),
            )
            self.store.conn.execute(
                """
                UPDATE encounter_candidates
                SET status='CHATTING',error='',updated_at=?,updated_at_epoch=?
                WHERE id=?
                """,
                (now.isoformat(), epoch_us(now), int(candidate_id)),
            )
        return {
            "id": int(cur.lastrowid),
            "candidate_id": int(candidate_id),
            "role": normalized_role,
            "content": text[:8000],
            "created_at": now.isoformat(),
        }

    def list_messages(self, candidate_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM (SELECT * FROM encounter_messages WHERE candidate_id=? ORDER BY created_at_epoch DESC,id DESC LIMIT ?) ORDER BY created_at_epoch,id",
                (int(candidate_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "candidate_id": int(row["candidate_id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def candidates_waiting_for_reply(self, *, limit: int = 50) -> list[int]:
        """Open candidates whose latest durable chat line is from the user."""

        with self.store._lock:
            rows = self.store.conn.execute(
                """
                SELECT c.id
                FROM encounter_candidates AS c
                JOIN encounter_messages AS m ON m.id=(
                    SELECT latest.id
                    FROM encounter_messages AS latest
                    WHERE latest.candidate_id=c.id
                    ORDER BY latest.created_at_epoch DESC,latest.id DESC
                    LIMIT 1
                )
                WHERE c.status IN ('NEW','SEEN','CHATTING') AND c.error='' AND m.role='USER'
                ORDER BY m.created_at_epoch,m.id
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def ensure_state(self, now: datetime, interval_minutes: float) -> dict[str, Any]:
        next_at = datetime.fromtimestamp(
            now.timestamp() + max(600.0, float(interval_minutes) * 60.0),
            tz=now.tzinfo,
        )
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO encounter_state(id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch) VALUES(1,?,?,?,?)",
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute("SELECT * FROM encounter_state WHERE id=1").fetchone()
        return dict(row)

    def set_next_opportunity(self, next_at: datetime, now: datetime) -> dict[str, Any]:
        with self.store._lock:
            self.store.conn.execute(
                """
                INSERT INTO encounter_state(id,next_opportunity_at,next_opportunity_at_epoch,updated_at,updated_at_epoch)
                VALUES(1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    next_opportunity_at=excluded.next_opportunity_at,
                    next_opportunity_at_epoch=excluded.next_opportunity_at_epoch,
                    updated_at=excluded.updated_at,updated_at_epoch=excluded.updated_at_epoch
                """,
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
            row = self.store.conn.execute("SELECT * FROM encounter_state WHERE id=1").fetchone()
        return dict(row)

    def claim_due(self, now: datetime, interval_minutes: float) -> int | None:
        state = self.ensure_state(now, interval_minutes)
        if int(state["next_opportunity_at_epoch"]) > epoch_us(now):
            return None
        next_at = datetime.fromtimestamp(
            now.timestamp() + max(600.0, float(interval_minutes) * 60.0),
            tz=now.tzinfo,
        )
        with self.store._lock:
            cur = self.store.conn.execute(
                "INSERT INTO encounter_runs(scheduled_for,scheduled_for_epoch,started_at,started_at_epoch,status) VALUES(?,?,?,?,?)",
                (
                    str(state["next_opportunity_at"]),
                    int(state["next_opportunity_at_epoch"]),
                    now.isoformat(),
                    epoch_us(now),
                    "RUNNING",
                ),
            )
            self.store.conn.execute(
                "UPDATE encounter_state SET next_opportunity_at=?,next_opportunity_at_epoch=?,updated_at=?,updated_at_epoch=? WHERE id=1",
                (next_at.isoformat(), epoch_us(next_at), now.isoformat(), epoch_us(now)),
            )
            self.store._maybe_commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        now: datetime,
        *,
        status: str,
        source_type: str | None = None,
        candidate_id: int | None = None,
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(status or "").strip().upper()
        if normalized not in {"CREATED", "SKIPPED_PENDING", "FAILED"}:
            raise ValueError("invalid encounter run status")
        with self.store._lock:
            self.store.conn.execute(
                """
                UPDATE encounter_runs SET completed_at=?,completed_at_epoch=?,source_type=?,status=?,
                    candidate_id=?,error=?,details_json=? WHERE id=?
                """,
                (
                    now.isoformat(),
                    epoch_us(now),
                    source_type,
                    normalized,
                    candidate_id,
                    str(error or "")[:2000],
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    int(run_id),
                ),
            )
            self.store.conn.execute(
                """
                UPDATE encounter_state SET last_opportunity_at=?,last_opportunity_at_epoch=?,
                    last_status=?,last_candidate_id=?,updated_at=?,updated_at_epoch=? WHERE id=1
                """,
                (
                    now.isoformat(),
                    epoch_us(now),
                    normalized,
                    candidate_id,
                    now.isoformat(),
                    epoch_us(now),
                ),
            )
            self.store._maybe_commit()

    def list_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM encounter_runs ORDER BY started_at_epoch DESC,id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["details"] = self._loads(item.pop("details_json", "{}"), {})
            items.append(item)
        return items

    def claim_daily_slot(self, now: datetime, kind: str) -> str | None:
        if kind not in {'CLASSIC', 'ORIGINAL'}:
            raise ValueError('invalid daily encounter kind')
        day = now.date().isoformat()
        key = f'{day}:{kind}'
        stamp = epoch_us(now)
        with self.store.transaction(immediate=True):
            if self.store.conn.execute('SELECT 1 FROM encounter_candidates WHERE daily_key=?', (key,)).fetchone():
                return None
            row = self.store.conn.execute('SELECT * FROM encounter_daily_slots WHERE slot_key=?', (key,)).fetchone()
            if row and (row['status'] == 'CREATED' or int(row['retry_epoch']) > stamp):
                return None
            self.store.conn.execute('''INSERT INTO encounter_daily_slots(slot_key,day,kind,status,updated_epoch,retry_epoch)
                VALUES(?,?,?,'RUNNING',?,?) ON CONFLICT(slot_key) DO UPDATE SET
                status='RUNNING',updated_epoch=excluded.updated_epoch,retry_epoch=excluded.retry_epoch''',
                (key, day, kind, stamp, epoch_us(now + timedelta(minutes=15))))
        return key

    def finish_daily_slot(self, key: str, now: datetime, *, candidate_id: int | None = None, error: str = ''):
        with self.store._lock:
            self.store.conn.execute('''UPDATE encounter_daily_slots SET status=?,candidate_id=?,error=?,
                updated_epoch=?,retry_epoch=? WHERE slot_key=?''',
                ('CREATED' if candidate_id else 'FAILED', candidate_id, error[:2000], epoch_us(now),
                 epoch_us(now + timedelta(minutes=10)), key))
            self.store._maybe_commit()

    def expire_unstarted(self, now: datetime) -> None:
        with self.store._lock:
            self.store.conn.execute('''UPDATE encounter_candidates SET status='EXPIRED',updated_at=?,updated_at_epoch=?
                WHERE status IN ('NEW','SEEN') AND created_at_epoch<?
                AND NOT EXISTS(SELECT 1 FROM encounter_messages m WHERE m.candidate_id=encounter_candidates.id)''',
                (now.isoformat(), epoch_us(now), epoch_us(now.replace(hour=0,minute=0,second=0,microsecond=0))))
            self.store._maybe_commit()

    def unopened_count(self) -> int:
        with self.store._lock:
            return int(self.store.conn.execute("SELECT COUNT(*) FROM encounter_candidates WHERE status IN ('NEW','SEEN')").fetchone()[0])

    def daily_slots(self, now: datetime) -> list[dict]:
        with self.store._lock:
            return [dict(row) for row in self.store.conn.execute('SELECT * FROM encounter_daily_slots WHERE day=? ORDER BY kind', (now.date().isoformat(),))]

    @staticmethod
    def _public_call(row) -> dict:
        return {key: value for key, value in dict(row).items() if key not in {'token', 'lease_epoch'}}

    def _expire_calls(self, now: datetime) -> None:
        self.store.conn.execute("UPDATE encounter_calls SET status='EXPIRED',updated_at=? WHERE status IN ('RINGING','ACCEPTED','CONNECTED') AND lease_epoch<=?", (now.isoformat(),epoch_us(now)))

    def begin_call(self, candidate_id: int, now: datetime) -> dict:
        token = secrets.token_urlsafe(32)
        identifier = uuid.uuid4().hex
        with self.store.transaction(immediate=True):
            self._expire_calls(now)
            candidate = self.get_candidate(candidate_id)
            if candidate is None:
                raise KeyError('encounter candidate not found')
            if candidate['status'] not in {'NEW','SEEN','CHATTING'}:
                raise ValueError('this encounter is already closed')
            if self.store.conn.execute("SELECT 1 FROM encounter_calls WHERE candidate_id=? AND status IN ('RINGING','ACCEPTED','CONNECTED')",(candidate_id,)).fetchone():
                raise ValueError('已有尚未结束的电话邀请，请先结束或稍后重试')
            self.store.conn.execute('INSERT INTO encounter_calls(id,candidate_id,status,token,created_at,updated_at,lease_epoch) VALUES(?,?,?,?,?,?,?)',
                (identifier,candidate_id,'RINGING',hashlib.sha256(token.encode()).hexdigest(),now.isoformat(),now.isoformat(),epoch_us(now+timedelta(seconds=90))))
        return {'id':identifier,'candidate_id':candidate_id,'session_token':token,'status':'RINGING'}

    def decide_call(self, identifier: str, accept: bool, reason: str, now: datetime) -> dict:
        with self.store.transaction():
            self._expire_calls(now)
            row=self.store.conn.execute('SELECT * FROM encounter_calls WHERE id=?',(identifier,)).fetchone()
            if row is None:
                raise KeyError('call not found')
            candidate=self.get_candidate(row['candidate_id'])
            if not candidate or candidate['status'] not in {'NEW','SEEN','CHATTING'}:
                self.store.conn.execute("UPDATE encounter_calls SET status='CANCELLED',updated_at=? WHERE id=?",(now.isoformat(),identifier))
                return {**self._public_call(row),'status':'CANCELLED'}
            if row['status']!='RINGING':
                raise ValueError('电话邀请已过期或结束')
            status='ACCEPTED' if accept else 'DECLINED'
            self.store.conn.execute('UPDATE encounter_calls SET status=?,reason=?,updated_at=?,lease_epoch=? WHERE id=?',
                (status,reason[:600],now.isoformat(),epoch_us(now+timedelta(seconds=90)),identifier))
            return {**self._public_call(row),'status':status,'reason':reason[:600],'updated_at':now.isoformat()}

    def update_call(self, identifier: str, token: str, action: str, now: datetime) -> dict:
        with self.store.transaction():
            self._expire_calls(now)
            row=self.store.conn.execute('SELECT * FROM encounter_calls WHERE id=?',(identifier,)).fetchone()
            if row is None:
                raise KeyError('call not found')
            if not hmac.compare_digest(str(row['token']),hashlib.sha256(str(token).encode()).hexdigest()):
                raise ValueError('通话会话凭据不匹配')
            candidate=self.get_candidate(row['candidate_id'])
            if not candidate or candidate['status'] not in {'NEW','SEEN','CHATTING'}:
                raise ValueError('this encounter is already closed')
            if action not in {'CONNECTED','ENDED','CHECK'}:
                raise ValueError('invalid call transition')
            if row['status'] not in {'ACCEPTED','CONNECTED'}:
                if action=='ENDED' and row['status']=='ENDED':
                    return self._public_call(row)
                raise ValueError('通话已结束、过期或未获接听')
            if action=='CHECK' and row['status']!='CONNECTED':
                raise ValueError('通话尚未连接')
            status=row['status'] if action=='CHECK' else action
            connected_at=row['connected_at'] or (now.isoformat() if action=='CONNECTED' else None)
            self.store.conn.execute('UPDATE encounter_calls SET status=?,updated_at=?,lease_epoch=?,connected_at=? WHERE id=?',
                (status,now.isoformat(),epoch_us(now+timedelta(seconds=90)),connected_at,identifier))
            return {**self._public_call(row),'status':status,'connected_at':connected_at,'updated_at':now.isoformat()}

    def list_calls(self, candidate_id: int, *, now: datetime | None = None) -> list[dict]:
        with self.store._lock:
            if now is not None:
                self._expire_calls(now)
                self.store._maybe_commit()
            return [self._public_call(row) for row in self.store.conn.execute('SELECT * FROM encounter_calls WHERE candidate_id=? ORDER BY created_at DESC,id DESC LIMIT 40',(candidate_id,))]

    def metrics(self, candidate_id: int) -> dict:
        with self.store._lock:
            counts={row['role']:int(row['n']) for row in self.store.conn.execute('SELECT role,COUNT(*) n FROM encounter_messages WHERE candidate_id=? GROUP BY role',(candidate_id,))}
            calls=self.store.conn.execute('SELECT COUNT(*) FROM encounter_calls WHERE candidate_id=? AND connected_at IS NOT NULL',(candidate_id,)).fetchone()[0]
        user,character=counts.get('USER',0),counts.get('CHARACTER',0)
        return {'user_messages':user,'character_replies':character,'mutual_turns':min(user,character),
            # A one-sided user message is not a shared experience; it becomes one
            # only after the character has actually answered. Connected calls
            # count on their own because the call itself is the shared event.
            'connected_calls':int(calls),'shared_experiences':int(user>0 and character>0)+int(calls),
            'familiarity':'正在认识' if user else '初次见面','trust':'暂无足够依据',
            'basis':'仅统计持久化消息和已连接的通话；不计算好感度，也不据次数推断信任。'}
