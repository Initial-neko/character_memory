"""One authoritative domain for Direct conversation facts (W3, UNIFIED).

Direct and Group visible conversation facts both live in ``conversation_events``
after this module exists. They stay distinguishable by the owning
``conversations.type``:

* ``DIRECT`` rows are the character-private conversation domain and keep the
  ``FactRef.kind == "EVENT"`` contract;
* ``GROUP`` rows are the shared-group domain and keep
  ``FactRef.kind == "CONVERSATION_EVENT"``.

The physical move is deliberately not a runtime mode. There is no cutover
switch and no dual read: every product write goes through
:class:`ConversationFactWriter`, and every product read of a Direct
conversation fact goes through :class:`ConversationFactReader`. Legacy rows in
``events`` are preserved forever, but they are no longer a source of truth for
new facts. They stay readable through their ``conversation_fact_aliases`` row so
old receipts, traces and Memory provenance keep resolving.

Canonical Direct conversation id::

    direct-{character_id}-{sha256(character_id + "\\0" + raw_id)[:32]}

with ``direct-{character_id}-legacy-unscoped`` for facts that never carried a
``metadata.conversation_id``. Canonical fact id for migrated rows is
``10**15 + legacy_event_id`` so a bare integer can be split by range without
losing JavaScript number precision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from datetime import datetime
import sqlite3
from typing import Any, Iterable

from character_memory.domain.facts import FactRef
from character_memory.domain.models import Event, EventType
from character_memory.time_utils import epoch_us, parse_datetime


logger = logging.getLogger("character_memory.storage.conversation_facts")

# 10**15. Chosen over 2**62 so ids stay below JavaScript's 2**53-1 safe integer
# bound; see the ADR for the full argument.
CONVERSATION_FACT_ID_FLOOR = 1_000_000_000_000_000
JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991

DIRECT_MESSAGE_TYPES = frozenset({EventType.USER_MESSAGE.value, EventType.CHARACTER_MESSAGE.value})
UNSCOPED_SUFFIX = "legacy-unscoped"


class LegacyDirectWriteClosed(RuntimeError):
    """A Direct conversation fact was about to be written to the legacy table."""


class FactSchemaIncomplete(RuntimeError):
    """UNIFIED reads found no DIRECT conversation domain; fail closed."""


def canonical_direct_conversation_id(character_id: str, raw_conversation_id: str | None) -> str:
    """Return the stable Direct conversation id for one (character, raw) pair."""
    owner = str(character_id)
    raw = "" if raw_conversation_id is None else str(raw_conversation_id)
    if not raw.strip():
        return f"direct-{owner}-{UNSCOPED_SUFFIX}"
    digest = hashlib.sha256((owner + "\0" + raw).encode("utf-8")).hexdigest()[:32]
    return f"direct-{owner}-{digest}"


def legacy_target_id(legacy_event_id: int) -> int:
    return CONVERSATION_FACT_ID_FLOOR + int(legacy_event_id)


def is_unified_fact_id(value: int | None) -> bool:
    return value is not None and int(value) >= CONVERSATION_FACT_ID_FLOOR


def metadata_sha256(raw: str | None) -> str:
    """Canonical hash used by aliases; mirrors the migration script contract."""
    text = raw if raw is not None else "{}"
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    normalized = json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_sha256(content: str | None) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def ensure_writable_event(event: Event) -> None:
    """Runtime guard: Direct chat facts may never enter the legacy ``events`` table.

    Every message write has one owner, :class:`ConversationFactWriter`. Migration
    and eval-fixture code uses ``SQLiteStore.append_legacy_event`` explicitly,
    which is a different, allow-listed call.
    """
    if event.event_type.value in DIRECT_MESSAGE_TYPES:
        raise LegacyDirectWriteClosed(
            "Direct conversation facts must be written through ConversationFactWriter"
        )


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS conversations(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER,
    updated_at TEXT NOT NULL,
    updated_at_epoch INTEGER,
    archived_at TEXT,
    archived_at_epoch INTEGER
);

CREATE TABLE IF NOT EXISTS conversation_members(
    conversation_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    joined_at_epoch INTEGER,
    PRIMARY KEY(conversation_id, actor_type, actor_id)
);

CREATE TABLE IF NOT EXISTS conversation_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    event_time_epoch INTEGER,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversation_fact_aliases(
    legacy_kind TEXT NOT NULL CHECK(legacy_kind='EVENT'),
    legacy_id INTEGER NOT NULL,
    character_id TEXT NOT NULL,
    legacy_conversation_id TEXT,
    conversation_id TEXT NOT NULL,
    conversation_event_id INTEGER UNIQUE,
    content_sha256 TEXT NOT NULL,
    metadata_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('MIGRATED','QUARANTINED')),
    reason TEXT NOT NULL DEFAULT '',
    migrated_at TEXT NOT NULL,
    PRIMARY KEY(legacy_kind, legacy_id)
);

CREATE TABLE IF NOT EXISTS conversation_fact_migrations(
    migration_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_legacy_id INTEGER NOT NULL DEFAULT 0,
    counts_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS conversation_fact_quarantine(
    legacy_kind TEXT NOT NULL CHECK(legacy_kind='EVENT'),
    legacy_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    raw_row_json TEXT NOT NULL,
    raw_metadata TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(legacy_kind, legacy_id)
);

CREATE TABLE IF NOT EXISTS conversation_fact_cutover(
    id INTEGER PRIMARY KEY CHECK(id=1),
    mode TEXT NOT NULL CHECK(mode IN ('LEGACY','SHADOW','UNIFIED')),
    migration_id TEXT NOT NULL,
    database_epoch TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_type_updated
    ON conversations(type, updated_at_epoch DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_fact_alias_canonical
    ON conversation_fact_aliases(conversation_event_id);
CREATE INDEX IF NOT EXISTS idx_conversation_events_type_time
    ON conversation_events(id, event_type, event_time_epoch);
"""


class ConversationFactSchema:
    """Creates the empty migration ledger and reserves the canonical id range.

    This is empty-schema DDL only. It never scans ``events``, never backfills and
    never switches anything: the formal migration is an explicit operator step.
    """

    def __init__(self, store):
        self.store = store

    def ensure(self) -> None:
        # One flag per Store object: readers are constructed per request and
        # must not pay a schema probe each time.
        if getattr(self.store, "_conversation_facts_ready", False):
            return
        with self.store._lock:
            if getattr(self.store, "_conversation_facts_ready", False):
                return
            row = self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_fact_aliases'"
            ).fetchone()
            if row is None:
                self.store.apply_schema_migration(
                    "conversation/004-conversation-fact-unified",
                    self._create,
                    immediate=True,
                )
            self._reserve_ids()
            self.store._conversation_facts_ready = True

    def _create(self) -> None:
        self.store.conn.executescript(_SCHEMA_DDL)

    def _reserve_ids(self) -> None:
        """Keep every future ``conversation_events.id`` inside the unified range.

        Group messages are AUTOINCREMENT too. Without reserving the sequence a
        fresh database would hand out small ids that collide with the
        ``FLOOR + legacy_id`` alias contract.
        """
        with self.store._lock:
            exists = self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            if exists is None:
                # AUTOINCREMENT bookkeeping appears with the first insert.
                return
            # Idempotent and safe under a concurrent writer: OR IGNORE covers the
            # missing row, the guarded UPDATE only ever raises the cursor.
            self.store.conn.execute(
                "INSERT OR IGNORE INTO sqlite_sequence(name,seq) VALUES('conversation_events',?)",
                (CONVERSATION_FACT_ID_FLOOR,),
            )
            self.store.conn.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name='conversation_events' AND seq<?",
                (CONVERSATION_FACT_ID_FLOOR, CONVERSATION_FACT_ID_FLOOR),
            )
            self.store._maybe_commit()


class ConversationFactWriter:
    """The only writer for Direct conversation facts (W3 UNIFIED).

    ``append_direct`` must run inside the caller's transaction when the caller
    needs the fact and its receipt/delivery to commit together. It participates
    in the store's nested transaction depth instead of opening its own.
    """

    def __init__(self, store):
        self.store = store
        ConversationFactSchema(store).ensure()

    # -- Direct -----------------------------------------------------------

    def append_direct(
        self,
        event: Event,
        *,
        owner_character_id: str | None = None,
        raw_conversation_id: str | None = None,
    ) -> Event:
        if event.event_type.value not in DIRECT_MESSAGE_TYPES:
            raise ValueError("append_direct only accepts USER_MESSAGE or CHARACTER_MESSAGE")
        owner = str(owner_character_id or event.character_id)
        if not owner.strip():
            raise ValueError("a Direct conversation fact requires an owner character")
        metadata = dict(event.metadata or {})
        raw = raw_conversation_id if raw_conversation_id is not None else metadata.get("conversation_id")
        canonical = canonical_direct_conversation_id(owner, raw)
        stamp = epoch_us(event.event_time)
        is_user = event.event_type == EventType.USER_MESSAGE
        actor_type, actor_id = ("USER", "user") if is_user else ("CHARACTER", owner)
        # Use the store's transaction owner: a failed implicit write (unique
        # constraint, SQLITE_BUSY) must roll back instead of leaving an open
        # deferred transaction on this connection, and a caller already inside a
        # larger transaction keeps its atomic scope.
        with self.store.transaction():
            with self.store._lock:
                self._ensure_direct_conversation(owner, canonical, event.event_time, stamp)
                cur = self.store.conn.execute(
                    "INSERT INTO conversation_events(conversation_id,turn_id,actor_type,actor_id,event_type,"
                    "event_time,event_time_epoch,content,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (canonical, "", actor_type, actor_id, event.event_type.value,
                     event.event_time.isoformat(), stamp, event.content,
                     json.dumps(metadata, ensure_ascii=False)),
                )
                event_id = int(cur.lastrowid)
                turn_id = self._turn_id(
                    event_type=event.event_type.value,
                    canonical=canonical,
                    owner=owner,
                    event_id=event_id,
                    source_event_id=metadata.get("source_event_id"),
                )
                self.store.conn.execute(
                    "UPDATE conversation_events SET turn_id=? WHERE id=?", (turn_id, event_id)
                )
                if not bool(metadata.get("hidden")):
                    self.store.conn.execute(
                        "UPDATE conversations SET updated_at=?,updated_at_epoch=? WHERE id=?",
                        (event.event_time.isoformat(), stamp, canonical),
                    )
        return event.model_copy(update={"id": event_id, "character_id": owner})

    def _ensure_direct_conversation(self, owner: str, canonical: str, at: datetime, stamp: int) -> None:
        iso = at.isoformat()
        self.store.conn.execute(
            "INSERT OR IGNORE INTO conversations(id,type,name,created_at,created_at_epoch,updated_at,"
            "updated_at_epoch,archived_at,archived_at_epoch) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
            (canonical, "DIRECT", owner, iso, stamp, iso, stamp),
        )
        for position, (actor_type, actor_id) in enumerate((("USER", "user"), ("CHARACTER", owner))):
            self.store.conn.execute(
                "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_type,actor_id,position,"
                "joined_at,joined_at_epoch) VALUES(?,?,?,?,?,?)",
                (canonical, actor_type, actor_id, position, iso, stamp),
            )

    def _turn_id(self, *, event_type: str, canonical: str, owner: str, event_id: int,
                 source_event_id: Any) -> str:
        if event_type == EventType.USER_MESSAGE.value:
            return f"direct-turn-ce-{event_id}"
        parent = self._resolve_source_event(owner, canonical, source_event_id)
        if parent is not None:
            return f"direct-turn-ce-{parent}"
        return f"direct-turn-ce-{event_id}"

    def _resolve_source_event(self, owner: str, canonical: str, source_event_id: Any) -> int | None:
        if source_event_id is None or isinstance(source_event_id, bool):
            return None
        try:
            identifier = int(source_event_id)
        except (TypeError, ValueError):
            return None
        row = self.store.conn.execute(
            "SELECT e.id FROM conversation_events e WHERE e.id=? AND e.conversation_id=? "
            "AND e.event_type='USER_MESSAGE' AND e.actor_type='USER'",
            (identifier, canonical),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        if identifier < CONVERSATION_FACT_ID_FLOOR:
            alias = self.store.conn.execute(
                "SELECT conversation_event_id,character_id,conversation_id FROM conversation_fact_aliases "
                "WHERE legacy_kind='EVENT' AND legacy_id=? AND status='MIGRATED'",
                (identifier,),
            ).fetchone()
            if (alias is not None and alias["conversation_event_id"] is not None
                    and alias["character_id"] == owner and alias["conversation_id"] == canonical):
                return int(alias["conversation_event_id"])
        return None

    # -- Metadata updates -------------------------------------------------

    def update_direct_metadata(
        self,
        event_id: int,
        metadata: dict[str, Any],
        *,
        character_id: str,
        raw_conversation_id: str | None,
    ) -> Event | None:
        """Replace metadata on one Direct fact, refusing a same-numbered stranger.

        Direct ids and Group ids used to be two overlapping AUTOINCREMENT
        sequences. The conversation identity check is what stops a voice update
        for one conversation from rewriting an unrelated row.
        """
        canonical = canonical_direct_conversation_id(character_id, raw_conversation_id)
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE e.id=? AND e.conversation_id=? AND c.type='DIRECT'",
                (int(event_id), canonical),
            ).fetchone()
            target_id = int(event_id)
            if row is None and int(event_id) < CONVERSATION_FACT_ID_FLOOR:
                alias = self.store.conn.execute(
                    "SELECT conversation_event_id FROM conversation_fact_aliases "
                    "WHERE legacy_kind='EVENT' AND legacy_id=? AND status='MIGRATED'",
                    (int(event_id),),
                ).fetchone()
                if alias is not None and alias["conversation_event_id"] is not None:
                    target_id = int(alias["conversation_event_id"])
                    row = self.store.conn.execute(
                        "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                        "WHERE e.id=? AND e.conversation_id=? AND c.type='DIRECT'",
                        (target_id, canonical),
                    ).fetchone()
            if row is None:
                logger.warning(
                    "conversation_facts.metadata_provenance_mismatch event_id=%s character=%s",
                    event_id, character_id,
                )
                return None
            self.store.conn.execute(
                "UPDATE conversation_events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), target_id),
            )
            self.store._maybe_commit()
            refreshed = self.store.conn.execute(
                "SELECT e.* FROM conversation_events e WHERE e.id=?", (target_id,)
            ).fetchone()
        return _direct_event_from_row(refreshed, character_id)

    # -- Group ------------------------------------------------------------

    def append_group(self, event):
        """Delegate shared facts to the existing Group primitive."""
        from character_memory.group_store import GroupRepository

        return GroupRepository(self.store).append_event(event)


class ConversationFactReader:
    """Read model for the Direct conversation domain."""

    def __init__(self, store):
        self.store = store
        ConversationFactSchema(store).ensure()

    # -- row helpers ------------------------------------------------------

    @staticmethod
    def _metadata(raw) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {"metadata_status": "INVALID"}
        return value if isinstance(value, dict) else {"metadata_status": "INVALID"}

    def _owner_for_conversation(self, conversation_id: str) -> str | None:
        row = self.store.conn.execute(
            "SELECT actor_id FROM conversation_members WHERE conversation_id=? AND actor_type='CHARACTER' "
            "ORDER BY position LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return str(row["actor_id"]) if row is not None else None

    def get_canonical_event(self, event_id: int) -> Event | None:
        """Read one unified Direct fact by id. Group rows are not exposed here."""
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE e.id=? AND c.type='DIRECT'",
                (int(event_id),),
            ).fetchone()
            if row is None:
                return None
            owner = self._owner_for_conversation(row["conversation_id"])
        if owner is None:
            return None
        return _direct_event_from_row(row, owner)

    def aliased_legacy_id(self, canonical_id: int) -> int | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT legacy_id FROM conversation_fact_aliases "
                "WHERE conversation_event_id=? AND status='MIGRATED'",
                (int(canonical_id),),
            ).fetchone()
        return int(row["legacy_id"]) if row is not None else None

    def canonical_id_for_legacy(self, legacy_id: int) -> int | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT conversation_event_id FROM conversation_fact_aliases "
                "WHERE legacy_kind='EVENT' AND legacy_id=? AND status='MIGRATED'",
                (int(legacy_id),),
            ).fetchone()
        if row is None or row["conversation_event_id"] is None:
            return None
        return int(row["conversation_event_id"])

    def get_direct_row(self, character_id: str, event_id: int) -> dict | None:
        """Resolve one Direct fact row for provenance/relationship readers.

        Unified ids read the shared table; legacy ids use their alias and fall
        back to the preserved ``events`` row when the migration has not imported
        that id.
        """
        identifier = int(event_id)
        canonical = self.canonical_id_for_legacy(identifier) if not is_unified_fact_id(identifier) else identifier
        if canonical is not None:
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                    "WHERE e.id=? AND c.type='DIRECT'",
                    (canonical,),
                ).fetchone()
                owner = self._owner_for_conversation(row["conversation_id"]) if row is not None else None
            if row is not None and owner == character_id:
                return {
                    "id": int(row["id"]),
                    "character_id": owner,
                    "event_type": str(row["event_type"]),
                    "event_time": str(row["event_time"]),
                    "event_time_epoch": row["event_time_epoch"],
                    "content": str(row["content"]),
                    "metadata_json": row["metadata_json"],
                    "conversation_id": str(row["conversation_id"]),
                    "legacy_event_id": int(event_id) if not is_unified_fact_id(identifier) else None,
                }
            return None
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT id,character_id,event_type,event_time,event_time_epoch,content,metadata_json "
                "FROM events WHERE id=? AND character_id=? AND event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')",
                (identifier, character_id),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    # -- resolution -------------------------------------------------------

    def resolve_direct(self, ref: FactRef, *, character_id: str | None = None) -> Event | None:
        """Resolve a Direct fact reference, including pre-migration ids."""
        if ref.kind != "EVENT":
            raise ValueError("Direct facts use FactRef.kind='EVENT'")
        identifier = int(ref.id)
        if is_unified_fact_id(identifier):
            event = self.get_canonical_event(identifier)
            if event is None:
                return None
            if character_id is not None and event.character_id != character_id:
                return None
            return event
        canonical = self.canonical_id_for_legacy(identifier)
        if canonical is not None:
            event = self.get_canonical_event(canonical)
            if event is None:
                return None
            if character_id is not None and event.character_id != character_id:
                return None
            return event
        # Unmigrated legacy row: preserved, read-only, same content contract.
        with self.store._lock:
            row = self.store.conn.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone()
        if row is None or row["event_time_epoch"] is None:
            return None
        if row["event_type"] not in DIRECT_MESSAGE_TYPES:
            return None
        event = _legacy_event_from_row(row)
        if character_id is not None and event.character_id != character_id:
            return None
        return event

    # -- history ----------------------------------------------------------

    # A fact has exactly one authoritative source: a migrated row is read from
    # the unified table, and only a row the migration has not imported yet falls
    # back to its preserved legacy copy. Both are never read for the same fact,
    # so history cannot double-count.
    _LEGACY_DIRECT = (
        "SELECT e.* FROM events e WHERE e.character_id=? AND e.event_time_epoch IS NOT NULL "
        "AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE') "
        "AND NOT EXISTS (SELECT 1 FROM conversation_fact_aliases a WHERE a.legacy_kind='EVENT' "
        "AND a.legacy_id=e.id AND a.status='MIGRATED')"
    )
    _UNIFIED_DIRECT = (
        "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
        "WHERE c.type='DIRECT' AND e.event_time_epoch IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
        "AND m.actor_type='CHARACTER' AND m.actor_id=?)"
    )

    def _direct_history_rows(self, character_id: str, *, limit: int,
                             before_epoch: int | None = None,
                             before_id: int | None = None,
                             anchored: tuple[int, int] | None = None,
                             event_type: str | None = None) -> list[tuple[int, int, Event]]:
        """Newest-first Direct conversation rows from both sources, merged.

        ``anchored`` is a resolved ``(event_time_epoch, id)`` cursor that is
        already known to exist in one of the two spaces; it is compared against
        the canonical id so a preserved legacy id cannot collide with it.
        """
        types = [event_type] if event_type else list(DIRECT_MESSAGE_TYPES)
        marks = ",".join("?" for _ in types)
        unified_sql = (
            "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            "WHERE c.type='DIRECT' AND e.event_time_epoch IS NOT NULL "
            f"AND e.event_type IN ({marks}) "
            "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
            "AND m.actor_type='CHARACTER' AND m.actor_id=?)"
        )
        unified_args: list = [*types, character_id]
        # Same source rule as `_UNIFIED_DIRECT`: an imported row is read only from
        # the unified table, an unimported one only from the preserved log.
        legacy_sql = (
            "SELECT e.* FROM events e WHERE e.character_id=? AND e.event_time_epoch IS NOT NULL "
            f"AND e.event_type IN ({marks}) "
            "AND NOT EXISTS (SELECT 1 FROM conversation_fact_aliases a WHERE a.legacy_kind='EVENT' "
            "AND a.legacy_id=e.id AND a.status='MIGRATED')"
        )
        legacy_args: list = [character_id, *types]
        if before_epoch is not None and anchored is not None:
            anchored_id = anchored[1]
            unified_sql += " AND (e.event_time_epoch<? OR (e.event_time_epoch=? AND e.id<?))"
            unified_args.extend([before_epoch, before_epoch, anchored_id])
            legacy_sql += " AND (e.event_time_epoch<? OR (e.event_time_epoch=? AND e.id<?))"
            legacy_args.extend([before_epoch, before_epoch, before_id])
        unified_sql += " ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT ?"
        unified_args.append(limit)
        legacy_sql += " ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT ?"
        legacy_args.append(limit)
        rows: list[tuple[int, int, Event]] = []
        with self.store._lock:
            for row in self.store.conn.execute(unified_sql, unified_args):
                owner = self._owner_for_conversation(row["conversation_id"]) or character_id
                rows.append((int(row["event_time_epoch"]), int(row["id"]),
                             _conversation_row_to_event(row, owner)))
            for row in self.store.conn.execute(legacy_sql, legacy_args):
                rows.append((int(row["event_time_epoch"]), int(row["id"]), _legacy_event_from_row(row)))
        rows.sort(key=lambda item: (item[0], item[1]))
        return rows[-limit:]

    def list_page(self, character_id: str, *, limit: int = 50, before_id: int | None = None):
        from character_memory.storage.chat_history import ChatHistoryPage

        page_size = max(1, min(int(limit), 100))
        with self.store._lock:
            anchored = None
            if before_id is not None:
                anchored = self._page_cursor(character_id, int(before_id))
                if anchored is None:
                    return ChatHistoryPage([], False, None)
            rows = self._direct_history_rows(
                character_id, limit=page_size + 1,
                before_epoch=anchored[0] if anchored else None,
                before_id=anchored[1] if anchored else None,
                anchored=anchored,
            )
        has_more = len(rows) > page_size
        # The query fetches newest-first, `_direct_history_rows` returns the
        # newest window in chronological (ascending) order. Drop the oldest
        # overflow row, which only exists to answer `has_more`.
        rows = rows[-page_size:] if has_more else rows
        events = [item[2] for item in rows]
        return ChatHistoryPage(events, has_more, int(events[0].id) if has_more and events else None)

    def _page_cursor(self, character_id: str, before_id: int) -> tuple[int, int] | None:
        """Resolve a page cursor to ``(epoch, canonical_or_legacy_id)``.

        The epoch lets both sources share one window. The id is the canonical id
        when the alias exists, otherwise the legacy id (which only the legacy
        query compares against).
        """
        canonical = self.canonical_id_for_legacy(before_id) if not is_unified_fact_id(before_id) else before_id
        if canonical is not None:
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT e.event_time_epoch,e.id FROM conversation_events e "
                    "JOIN conversations c ON c.id=e.conversation_id WHERE e.id=? AND c.type='DIRECT'",
                    (canonical,),
                ).fetchone()
            if row is not None and row["event_time_epoch"] is not None:
                return int(row["event_time_epoch"]), int(row["id"])
        if not is_unified_fact_id(before_id):
            with self.store._lock:
                legacy = self.store.conn.execute(
                    "SELECT event_time_epoch,id FROM events WHERE id=? AND character_id=? "
                    "AND event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE')",
                    (before_id, character_id),
                ).fetchone()
            if legacy is not None and legacy["event_time_epoch"] is not None:
                return int(legacy["event_time_epoch"]), int(legacy["id"])
        return None

    def list_chat_events(self, character_id: str, limit: int = 160) -> list[Event]:
        rows = self._direct_history_rows(character_id, limit=int(limit))
        return [item[2] for item in rows]

    def latest_direct_message_id(self, character_id: str,
                                 event_types: Iterable[str]) -> int:
        """Newest inserted Direct conversation fact of the given types.

        Callers that need an arrival-order watermark use this next to the legacy
        ``events`` watermark; the two id spaces are compared as two counters,
        never as one number, because only each space is monotonic on its own.
        """
        types = [str(item) for item in event_types if str(item) in DIRECT_MESSAGE_TYPES]
        if not types:
            return 0
        marks = ",".join("?" for _ in types)
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT MAX(e.id) AS id FROM conversation_events e "
                "JOIN conversations c ON c.id=e.conversation_id "
                f"WHERE c.type='DIRECT' AND e.event_type IN ({marks}) "
                "AND EXISTS (SELECT 1 FROM conversation_members m "
                "WHERE m.conversation_id=e.conversation_id "
                "AND m.actor_type='CHARACTER' AND m.actor_id=?)",
                [*types, character_id],
            ).fetchone()
        return int(row["id"] or 0) if row is not None else 0

    def latest_fact_id(self, character_id: str, raw_conversation_id: str | None,
                       event_types: Iterable[str]) -> int | None:
        """Newest inserted fact of the given types in one Direct conversation.

        Conversation facts read the shared table; inputs that are not
        conversation facts (TIME_TICK, VISUAL_OBSERVATION, PROACTIVE_INTENT) still
        live in ``events`` and keep their legacy id. The result is returned as
        written: callers compare it with ``fact_order_key`` when the two spaces
        can meet.
        """
        types = tuple(str(item) for item in event_types if str(item))
        if not types:
            return None
        canonical = canonical_direct_conversation_id(character_id, raw_conversation_id)
        chat_types = [item for item in types if item in DIRECT_MESSAGE_TYPES]
        legacy_types = [item for item in types if item not in DIRECT_MESSAGE_TYPES]
        candidates: list[int] = []
        with self.store._lock:
            if chat_types:
                marks = ",".join("?" for _ in chat_types)
                row = self.store.conn.execute(
                    "SELECT MAX(e.id) AS id FROM conversation_events e "
                    "JOIN conversations c ON c.id=e.conversation_id "
                    f"WHERE c.type='DIRECT' AND c.id=? AND e.event_type IN ({marks})",
                    [canonical, *chat_types],
                ).fetchone()
                if row is not None and row["id"] is not None:
                    candidates.append(int(row["id"]))
            if legacy_types:
                marks = ",".join("?" for _ in legacy_types)
                row = self.store.conn.execute(
                    "SELECT MAX(id) AS id FROM events WHERE character_id=? "
                    f"AND event_type IN ({marks}) AND json_extract(metadata_json,'$.conversation_id')=?",
                    [character_id, *legacy_types, raw_conversation_id],
                ).fetchone()
                if row is not None and row["id"] is not None:
                    candidates.append(int(row["id"]))
        if not candidates:
            return None
        # Return the raw id (callers compare equality with the event they emitted);
        # pick the newest by arrival order across the two id spaces.
        return max(candidates, key=self.fact_order_key)

    def fact_order_key(self, event_id: int) -> int:
        """A comparable arrival-order key across both id spaces.

        Every fact written after the cutover is a unified id, and every preserved
        legacy row predates it, so mapping an imported legacy id to its canonical
        id (and leaving an unmigrated legacy id alone) preserves both arrival
        order within a space and the spaces' relative age. Only supersession
        comparisons use this; ownership checks stay separate.
        """
        identifier = int(event_id)
        if is_unified_fact_id(identifier):
            return identifier
        canonical = self.canonical_id_for_legacy(identifier)
        return canonical if canonical is not None else identifier

    def newest_direct_input(
        self,
        character_id: str,
        raw_conversation_id: str | None,
        *,
        visual: bool,
        at: datetime | None = None,
    ) -> tuple[int, int, str] | None:
        """Newest Direct input that can supersede an in-flight reaction.

        A user message always wins, and (for a screen reaction) a later visual
        observation also wins. Chat facts and non-chat inputs live in different
        id spaces, so the cross-space comparison is by event time; within a space
        arrival order (id) breaks ties. Returned as ``(epoch, id, space)``.

        When ``at`` is supplied it is the trusted server clock: facts dated after
        it have not happened yet and cannot supersede a generation running now.
        Caller-controlled future timestamps must not stall delivery (P15-F1).
        """
        canonical = canonical_direct_conversation_id(character_id, raw_conversation_id)
        cutoff = epoch_us(at) if at is not None else None
        rows: list[tuple[int, int, str]] = []
        with self.store._lock:
            chat_sql = (
                "SELECT e.id,e.event_time_epoch FROM conversation_events e "
                "JOIN conversations c ON c.id=e.conversation_id "
                "WHERE c.type='DIRECT' AND c.id=? AND e.event_type='USER_MESSAGE' "
                "AND e.event_time_epoch IS NOT NULL"
            )
            chat_params = [canonical]
            if cutoff is not None:
                chat_sql += " AND e.event_time_epoch<=?"
                chat_params.append(cutoff)
            row = self.store.conn.execute(
                chat_sql + " ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT 1", chat_params
            ).fetchone()
            if row is not None and row["event_time_epoch"] is not None:
                rows.append((int(row["event_time_epoch"]), int(row["id"]), "CHAT"))
            if visual:
                visual_sql = (
                    "SELECT id,event_time_epoch FROM events WHERE character_id=? "
                    "AND event_type='VISUAL_OBSERVATION' AND event_time_epoch IS NOT NULL "
                    "AND json_extract(metadata_json,'$.conversation_id')=? "
                )
                visual_params = [character_id, raw_conversation_id]
                if cutoff is not None:
                    visual_sql += " AND event_time_epoch<=?"
                    visual_params.append(cutoff)
                row = self.store.conn.execute(
                    visual_sql + " ORDER BY event_time_epoch DESC,id DESC LIMIT 1", visual_params
                ).fetchone()
                if row is not None and row["event_time_epoch"] is not None:
                    rows.append((int(row["event_time_epoch"]), int(row["id"]), "VISUAL"))
        if not rows:
            return None
        return max(rows)

    def direct_input_identity(self, character_id: str, raw_conversation_id: str | None,
                              event_id: int) -> tuple[int, int, str] | None:
        """Identity tuple of one Direct input, in the same shape as newest_direct_input."""
        identifier = int(event_id)
        canonical = identifier if is_unified_fact_id(identifier) else self.canonical_id_for_legacy(identifier)
        if canonical is not None:
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT event_time_epoch FROM conversation_events WHERE id=?",
                    (canonical,),
                ).fetchone()
            if row is None or row["event_time_epoch"] is None:
                return None
            return int(row["event_time_epoch"]), canonical, "CHAT"
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT event_type,event_time_epoch FROM events WHERE id=?", (identifier,)
            ).fetchone()
        if row is None or row["event_time_epoch"] is None:
            return None
        space = "VISUAL" if row["event_type"] == "VISUAL_OBSERVATION" else "CHAT"
        return int(row["event_time_epoch"]), identifier, space

    def list_response_events(self, character_id: str, source_event_id: int) -> list[Event]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE c.type='DIRECT' AND e.event_type='CHARACTER_MESSAGE' "
                "AND CAST(json_extract(e.metadata_json,'$.source_event_id') AS INTEGER)=? "
                "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
                "AND m.actor_type='CHARACTER' AND m.actor_id=?) ORDER BY e.id",
                (int(source_event_id), character_id),
            ).fetchall()
        return [_direct_event_from_row(row, character_id) for row in rows]

    def count_character_messages(self, character_id: str) -> int:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS total FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE c.type='DIRECT' AND e.actor_type='CHARACTER' AND e.actor_id=?",
                (character_id,),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    # -- search -----------------------------------------------------------

    @staticmethod
    def _pattern(query: str) -> str:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def search(self, query: str, *, character_id: str | None = None, limit: int = 50) -> list[dict]:
        """Direct hits across both sources; one fact is never returned twice."""
        pattern = self._pattern(query.strip())
        page_size = max(1, min(int(limit), 50))
        sql = (
            "SELECT e.* FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
            "WHERE c.type='DIRECT' AND e.event_time_epoch IS NOT NULL AND e.event_type IN (?,?) "
            "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
            "AND m.actor_type='CHARACTER'"
        )
        args: list = [EventType.USER_MESSAGE.value, EventType.CHARACTER_MESSAGE.value]
        if character_id:
            sql += " AND m.actor_id=?"
            args.append(character_id)
        sql += (
            ") AND (COALESCE(json_extract(e.metadata_json,'$.display_text'),'') || ' ' || "
            "COALESCE(e.content,'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.sticker_label'),'') || ' ' || "
            "COALESCE(json_extract(e.metadata_json,'$.media_name'),'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.image_label'),'')) "
            "LIKE ? ESCAPE '\\' ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT ?"
        )
        args.extend([pattern, page_size])
        legacy_sql = self._LEGACY_DIRECT if character_id else (
            "SELECT e.* FROM events e WHERE e.event_time_epoch IS NOT NULL "
            "AND e.event_type IN ('USER_MESSAGE','CHARACTER_MESSAGE') "
            "AND NOT EXISTS (SELECT 1 FROM conversation_fact_aliases a WHERE a.legacy_kind='EVENT' "
            "AND a.legacy_id=e.id AND a.status='MIGRATED')"
        )
        legacy_args: list = [character_id] if character_id else []
        legacy_sql += (
            " AND (COALESCE(json_extract(e.metadata_json,'$.display_text'),'') || ' ' || "
            "COALESCE(e.content,'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.sticker_label'),'') || ' ' || "
            "COALESCE(json_extract(e.metadata_json,'$.media_name'),'') || ' ' || COALESCE(json_extract(e.metadata_json,'$.image_label'),'')) "
            "LIKE ? ESCAPE '\\' ORDER BY e.event_time_epoch DESC,e.id DESC LIMIT ?"
        )
        legacy_args.extend([pattern, page_size])
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
            legacy_rows = self.store.conn.execute(legacy_sql, legacy_args).fetchall()
            results = []
            for row in rows:
                owner = self._owner_for_conversation(row["conversation_id"])
                results.append(self._search_item(row, owner, "CHAT"))
            for row in legacy_rows:
                results.append(self._search_item(row, str(row["character_id"]), "LEGACY"))
        results.sort(key=lambda item: (item["event_time_epoch"], item["event_id"]), reverse=True)
        return results[:page_size]

    def _search_item(self, row, character_id: str | None, space: str) -> dict:
        metadata = self._metadata(row["metadata_json"])
        return {
            "scope": "DIRECT",
            "event_id": int(row["id"]),
            "character_id": character_id,
            "conversation_id": metadata.get("conversation_id"),
            "event_type": str(row["event_type"]),
            "event_time": str(row["event_time"]),
            "event_time_epoch": int(row["event_time_epoch"]),
            "content": str(
                metadata.get("display_text", row["content"])
                if row["event_type"] == EventType.USER_MESSAGE.value
                else row["content"]
            ),
            "metadata": metadata,
            "fact_source": space,
        }

    def jump_before(self, character_id: str, event_id: int, *, newer_context: int = 20) -> int | None:
        canonical = self.canonical_id_for_legacy(event_id) if not is_unified_fact_id(event_id) else int(event_id)
        if canonical is None:
            return None
        with self.store._lock:
            target = self.store.conn.execute(
                "SELECT e.event_time_epoch,e.id FROM conversation_events e "
                "JOIN conversations c ON c.id=e.conversation_id WHERE e.id=? AND c.type='DIRECT' "
                "AND e.event_time_epoch IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
                "AND m.actor_type='CHARACTER' AND m.actor_id=?)",
                (canonical, character_id),
            ).fetchone()
            if target is None:
                return None
            rows = self.store.conn.execute(
                "SELECT e.id FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
                "WHERE c.type='DIRECT' AND e.event_time_epoch IS NOT NULL AND e.event_type IN (?,?) "
                "AND EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=e.conversation_id "
                "AND m.actor_type='CHARACTER' AND m.actor_id=?) "
                "AND (e.event_time_epoch>? OR (e.event_time_epoch=? AND e.id>?)) "
                "ORDER BY e.event_time_epoch ASC,e.id ASC LIMIT ?",
                (
                    EventType.USER_MESSAGE.value,
                    EventType.CHARACTER_MESSAGE.value,
                    character_id,
                    target["event_time_epoch"],
                    target["event_time_epoch"],
                    target["id"],
                    max(1, int(newer_context)),
                ),
            ).fetchall()
        if len(rows) < newer_context:
            return None
        return int(rows[-1]["id"])

    def trace_sources(self, character_id: str, source_event_ids: list[int]) -> set[int]:
        ids = sorted({int(value) for value in source_event_ids if value is not None})
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self.store._lock:
            rows = self.store.conn.execute(
                f"SELECT source_event_id FROM runtime_traces WHERE character_id=? AND source_event_id IN ({placeholders})",
                [character_id, *ids],
            ).fetchall()
            found = {int(row["source_event_id"]) for row in rows}
            # Traces recorded before the migration are keyed by the legacy id.
            legacy = self.store.conn.execute(
                f"SELECT legacy_id,conversation_event_id FROM conversation_fact_aliases "
                f"WHERE conversation_event_id IN ({placeholders}) AND status='MIGRATED'",
                ids,
            ).fetchall()
            reverse = {int(row["conversation_event_id"]): int(row["legacy_id"]) for row in legacy}
            if reverse:
                legacy_ids = sorted(reverse.values())
                marks = ",".join("?" for _ in legacy_ids)
                legacy_rows = self.store.conn.execute(
                    f"SELECT source_event_id FROM runtime_traces WHERE character_id=? AND source_event_id IN ({marks})",
                    [character_id, *legacy_ids],
                ).fetchall()
                hit = {int(row["source_event_id"]) for row in legacy_rows}
                found.update(canonical for canonical, legacy_id in reverse.items() if legacy_id in hit)
        return found


def _metadata_from_raw(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"metadata_status": "INVALID"}
    return value if isinstance(value, dict) else {"metadata_status": "INVALID"}


def _direct_event_from_row(row, character_id: str) -> Event:
    return _conversation_row_to_event(row, character_id)


def _conversation_row_to_event(row, character_id: str) -> Event:
    """Adapt one ``conversation_events`` row to the domain ``Event`` shape."""
    return Event(
        id=int(row["id"]),
        character_id=character_id,
        event_type=EventType(str(row["event_type"])),
        event_time=parse_datetime(row["event_time"]),
        content=str(row["content"]),
        metadata=_metadata_from_raw(row["metadata_json"]),
    )


def _legacy_event_from_row(row) -> Event:
    return Event(
        id=int(row["id"]),
        character_id=str(row["character_id"]),
        event_type=EventType(str(row["event_type"])),
        event_time=parse_datetime(row["event_time"]),
        content=str(row["content"]),
        metadata=_metadata_from_raw(row["metadata_json"]),
    )


def as_event(row, character_id: str) -> Event:
    """Public adapter for callers that hold a raw conversation_events row."""
    return _direct_event_from_row(row, character_id)
