#!/usr/bin/env python
"""Backfill Direct conversation facts into ``conversation_events`` (W3).

This script is the only migration entry point. Nothing in the application calls
it: importing or starting the server never scans ``events``, never backfills and
never installs a trigger. See ``docs/current/CONVERSATION_FACT_MIGRATION.md``.

Contract summary
----------------
* Scope: ``USER_MESSAGE`` and ``CHARACTER_MESSAGE`` rows only. Every other event
  type keeps its existing owner and table.
* Target id: ``CONVERSATION_FACT_ID_FLOOR + events.id``; the alias ledger stores
  the mapping so old receipts, traces and Memory provenance keep resolving.
* The source rows are never updated or deleted.
* Batches are one transaction each and the checkpoint only advances after the
  commit, so a kill -9 resumes from the last committed batch.
* Re-running is a no-op when the alias, hashes and canonical ids all match; a
  mismatch aborts instead of overwriting.

Modes
-----
``--dry-run`` (default) reports counts and validates without writing anything.
``--apply`` writes and requires either an explicit ``--backup`` or
``--allow-unbacked`` for a throwaway copy.

Examples
--------
    # Inspect a copy (writes nothing):
    python scripts/migrate_conversation_facts.py --db /tmp/copy.db

    # Migrate the copy after an online backup:
    python scripts/migrate_conversation_facts.py --db /tmp/copy.db \
        --backup /tmp/copy.db.bak --apply

    # Formal cutover on the live database is an operator step; it also needs
    # `--cutover` to record mode=UNIFIED.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from character_memory.storage.conversation_facts import (  # noqa: E402
    CONVERSATION_FACT_ID_FLOOR,
    JS_SAFE_INTEGER_MAX,
    canonical_direct_conversation_id,
    content_sha256,
    legacy_target_id,
    metadata_sha256,
)

MESSAGE_TYPES = ("USER_MESSAGE", "CHARACTER_MESSAGE")
DEFAULT_BATCH_SIZE = 500


class MigrationAborted(RuntimeError):
    pass


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_metadata(raw: str | None) -> str:
    """Canonical JSON text so semantically equal metadata hashes equally."""
    if raw is None:
        return "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id,character_id,event_type,event_time,event_time_epoch,content,metadata_json "
        f"FROM events WHERE event_type IN ({','.join('?' for _ in MESSAGE_TYPES)}) ORDER BY id",
        MESSAGE_TYPES,
    ).fetchall()


def _all_event_types(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["event_type"]): int(row["total"])
        for row in conn.execute("SELECT event_type,COUNT(*) AS total FROM events GROUP BY event_type ORDER BY event_type")
    }


def source_fingerprint(conn: sqlite3.Connection) -> str:
    """Content fingerprint of every in-scope source row.

    Covers id, content, time and canonical metadata so a dry-run on one database
    cannot be used to authorise an ``--apply`` on a different one.
    """
    digest = hashlib.sha256()
    for row in _source_rows(conn):
        digest.update(
            "\x1f".join(
                str(value if value is not None else "")
                for value in (
                    row["id"],
                    row["character_id"],
                    row["event_type"],
                    row["event_time"],
                    row["content"],
                    _canonical_metadata(row["metadata_json"]),
                )
            ).encode("utf-8")
        )
        digest.update(b"\x1e")
    return digest.hexdigest()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _max_ids(conn: sqlite3.Connection) -> dict[str, int]:
    legacy = conn.execute(
        f"SELECT COALESCE(MAX(id),0) AS m FROM events WHERE event_type IN ({','.join('?' for _ in MESSAGE_TYPES)})",
        MESSAGE_TYPES,
    ).fetchone()["m"]
    conversations = 0
    if _has_table(conn, "conversation_events"):
        conversations = conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM conversation_events").fetchone()["m"]
    return {"max_legacy_message_id": int(legacy), "max_conversation_event_id": int(conversations)}


def _sequence_value(conn: sqlite3.Connection) -> int:
    if not _has_table(conn, "sqlite_sequence"):
        return 0
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_events'").fetchone()
    return int(row["seq"]) if row is not None else 0


def _assert_floor_contract(conn: sqlite3.Connection) -> dict[str, int]:
    """Enforce the reserved id range, allowing a previous run's own rows.

    A first run must see every existing ``conversation_events`` id below the
    floor. A re-run necessarily sees the target rows it wrote last time, so the
    check is "no id at or above the floor that this alias ledger cannot explain".
    """
    limits = _max_ids(conn)
    if limits["max_conversation_event_id"] >= CONVERSATION_FACT_ID_FLOOR:
        unexplained = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_events e WHERE e.id>=? AND NOT EXISTS ("
            "SELECT 1 FROM conversation_fact_aliases a WHERE a.conversation_event_id=e.id "
            "AND a.status='MIGRATED')",
            (CONVERSATION_FACT_ID_FLOOR,),
        ).fetchone()["n"]
        if int(unexplained):
            raise MigrationAborted(
                "conversation_events contains ids at or above the unified floor that no alias "
                "explains; this database was migrated by another process or corrupted"
            )
    highest = CONVERSATION_FACT_ID_FLOOR + limits["max_legacy_message_id"]
    if highest > JS_SAFE_INTEGER_MAX:
        raise MigrationAborted("reserved id range exceeds the JavaScript safe-integer bound")
    return limits


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Reuse the runtime DDL so a copy and a live database always share one shape.
    for statement in _schema_statements():
        conn.execute(statement)


def _schema_statements() -> list[str]:
    from character_memory.storage import conversation_facts as module

    return [stmt.strip() for stmt in module._SCHEMA_DDL.split(";") if stmt.strip()]


def _reserve_sequence(conn: sqlite3.Connection, max_legacy_id: int) -> int:
    target = CONVERSATION_FACT_ID_FLOOR + max_legacy_id
    conn.execute(
        "INSERT OR IGNORE INTO sqlite_sequence(name,seq) VALUES('conversation_events',?)", (target,)
    )
    conn.execute(
        "UPDATE sqlite_sequence SET seq=? WHERE name='conversation_events' AND seq<?", (target, target)
    )
    return _sequence_value(conn)


def _raw_conversation_id(raw_metadata: str | None) -> str | None:
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("conversation_id")
    return str(value) if isinstance(value, str) and value.strip() else None


def _turn_id(row: sqlite3.Row, canonical: str, source_ids: set[int]) -> str:
    legacy_id = int(row["id"])
    if row["event_type"] == "USER_MESSAGE":
        return f"direct-turn-legacy-{legacy_id}"
    source = _metadata_source_id(row["metadata_json"])
    if source is not None and source in source_ids:
        return f"direct-turn-legacy-{source}"
    return f"direct-turn-legacy-{legacy_id}"


def _metadata_source_id(raw_metadata: str | None) -> int | None:
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("source_event_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _media_warnings(conn: sqlite3.Connection, metadata: dict[str, Any]) -> list[str]:
    warnings = []
    if "media_id" in metadata:
        found = conn.execute("SELECT 1 FROM media_assets WHERE id=?", (metadata["media_id"],)).fetchone()
        if found is None:
            warnings.append("MISSING_MEDIA")
    return warnings


def _target_row_check(conn: sqlite3.Connection, row: sqlite3.Row, canonical: str) -> str:
    """Compare an already-written target row against its source. Returns a verdict."""
    target = conn.execute(
        "SELECT * FROM conversation_events WHERE id=?", (legacy_target_id(int(row["id"])),)
    ).fetchone()
    if target is None:
        return "TARGET_MISSING"
    if target["conversation_id"] != canonical or target["content"] != row["content"]:
        return "TARGET_MISMATCH"
    if target["event_time"] != row["event_time"] or target["event_time_epoch"] != row["event_time_epoch"]:
        return "TARGET_MISMATCH"
    if metadata_sha256(target["metadata_json"]) != metadata_sha256(row["metadata_json"]):
        return "TARGET_MISMATCH"
    return "OK"


def _batch(rows: list[sqlite3.Row], size: int) -> Iterable[list[sqlite3.Row]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _insert_conversation(conn: sqlite3.Connection, character_id: str, canonical: str,
                          at: str, stamp: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO conversations(id,type,name,created_at,created_at_epoch,updated_at,"
        "updated_at_epoch,archived_at,archived_at_epoch) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
        (canonical, "DIRECT", character_id, at, stamp, at, stamp),
    )
    for position, (actor_type, actor_id) in enumerate((("USER", "user"), ("CHARACTER", character_id))):
        conn.execute(
            "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_type,actor_id,position,"
            "joined_at,joined_at_epoch) VALUES(?,?,?,?,?,?)",
            (canonical, actor_type, actor_id, position, at, stamp),
        )


def _migrate_row(conn: sqlite3.Connection, row: sqlite3.Row, *, source_ids: set[int],
                 counts: dict[str, int], warnings: dict[str, int]) -> None:
    legacy_id = int(row["id"])
    character_id = str(row["character_id"] or "")
    raw_metadata = row["metadata_json"]
    raw_conversation_id = _raw_conversation_id(raw_metadata)
    canonical = canonical_direct_conversation_id(character_id, raw_conversation_id)
    metahash = metadata_sha256(raw_metadata)
    contenhash = content_sha256(row["content"])
    now = _utcnow()

    existing = conn.execute(
        "SELECT * FROM conversation_fact_aliases WHERE legacy_kind='EVENT' AND legacy_id=?",
        (legacy_id,),
    ).fetchone()
    if existing is not None:
        if (existing["content_sha256"] != contenhash or existing["metadata_sha256"] != metahash
                or existing["conversation_id"] != canonical or existing["character_id"] != character_id):
            raise MigrationAborted(
                f"legacy event {legacy_id} changed since it was migrated; refusing to overwrite its alias"
            )
        if existing["status"] == "MIGRATED":
            verdict = _target_row_check(conn, row, canonical)
            if verdict != "OK":
                raise MigrationAborted(f"legacy event {legacy_id}: {verdict}")
            counts["skipped"] += 1
        else:
            counts["skipped_quarantined"] += 1
        return

    def quarantine(reason: str) -> None:
        conn.execute(
            "INSERT INTO conversation_fact_aliases(legacy_kind,legacy_id,character_id,"
            "legacy_conversation_id,conversation_id,conversation_event_id,content_sha256,"
            "metadata_sha256,status,reason,migrated_at) VALUES('EVENT',?,?,?,?,NULL,?,?,?,?,?)",
            (legacy_id, character_id, raw_conversation_id, canonical, contenhash, metahash,
             "QUARANTINED", reason, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO conversation_fact_quarantine(legacy_kind,legacy_id,reason,"
            "raw_row_json,raw_metadata,created_at) VALUES('EVENT',?,?,?,?,?)",
            (legacy_id, reason, json.dumps(dict(row), ensure_ascii=False, default=str), raw_metadata, now),
        )
        counts["quarantined"] += 1
        counts[f"quarantined_{reason.lower()}"] = counts.get(f"quarantined_{reason.lower()}", 0) + 1

    if not character_id.strip():
        quarantine("MISSING_OWNER")
        return
    if row["event_time_epoch"] is None:
        # Existing history/search already hide NULL-epoch rows; the migration must
        # not make them visible for the first time.
        quarantine("INVALID_TIME")
        return

    try:
        metadata = json.loads(raw_metadata or "{}")
        metadata_ok = isinstance(metadata, dict)
    except (TypeError, ValueError):
        metadata, metadata_ok = {}, False
    if not metadata_ok:
        target_metadata: dict[str, Any] = {
            "metadata_status": "INVALID",
            "legacy_metadata_sha256": _sha(raw_metadata or ""),
        }
    else:
        target_metadata = metadata

    for warning in _media_warnings(conn, target_metadata):
        warnings[warning] = warnings.get(warning, 0) + 1
    if raw_conversation_id is None:
        warnings["MISSING_CONVERSATION_ID"] = warnings.get("MISSING_CONVERSATION_ID", 0) + 1
    if character_id != str(row["character_id"]):
        warnings["CHARACTER_ID_COERCED"] = warnings.get("CHARACTER_ID_COERCED", 0) + 1

    target_id = legacy_target_id(legacy_id)
    _insert_conversation(conn, character_id, canonical, row["event_time"], int(row["event_time_epoch"]))
    is_user = row["event_type"] == "USER_MESSAGE"
    conn.execute(
        "INSERT INTO conversation_events(id,conversation_id,turn_id,actor_type,actor_id,event_type,"
        "event_time,event_time_epoch,content,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            target_id,
            canonical,
            _turn_id(row, canonical, source_ids),
            "USER" if is_user else "CHARACTER",
            "user" if is_user else character_id,
            row["event_type"],
            row["event_time"],
            int(row["event_time_epoch"]),
            row["content"],
            json.dumps(target_metadata, ensure_ascii=False),
        ),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=?,updated_at_epoch=? WHERE id=? AND updated_at_epoch<?",
        (row["event_time"], int(row["event_time_epoch"]), canonical, int(row["event_time_epoch"])),
    )
    conn.execute(
        "INSERT INTO conversation_fact_aliases(legacy_kind,legacy_id,character_id,"
        "legacy_conversation_id,conversation_id,conversation_event_id,content_sha256,"
        "metadata_sha256,status,reason,migrated_at) VALUES('EVENT',?,?,?,?,?,?,?,?,?,?)",
        (legacy_id, character_id, raw_conversation_id, canonical, target_id, contenhash,
         metahash, "MIGRATED", "", now),
    )
    counts["migrated"] += 1


def _checkpoint(conn: sqlite3.Connection, migration_id: str, fingerprint: str,
                last_id: int, counts: dict[str, int], finished: bool) -> None:
    conn.execute(
        "INSERT INTO conversation_fact_migrations(migration_id,source_fingerprint,started_at,"
        "finished_at,last_legacy_id,counts_json,errors_json) VALUES(?,?,?,?,?,?,'[]') "
        "ON CONFLICT(migration_id) DO UPDATE SET finished_at=excluded.finished_at,"
        "last_legacy_id=excluded.last_legacy_id,counts_json=excluded.counts_json",
        (migration_id, fingerprint, _utcnow(), _utcnow() if finished else None, last_id,
         json.dumps(counts, sort_keys=True)),
    )


def _report(conn: sqlite3.Connection, rows: list[sqlite3.Row], fingerprint: str) -> dict[str, Any]:
    """Before/after counters used by the acceptance record.

    Safe on a database that has never run the migration: every target/ledger
    table is optional and simply reports zero.
    """
    total = len(rows)

    def count(sql: str, args: tuple = ()) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    has_aliases = _has_table(conn, "conversation_fact_aliases")
    has_conversations = _has_table(conn, "conversations")
    has_facts = _has_table(conn, "conversation_events")
    migrated = count(
        "SELECT COUNT(*) FROM conversation_fact_aliases WHERE legacy_kind='EVENT' AND status='MIGRATED'"
    ) if has_aliases else 0
    quarantined = count(
        "SELECT COUNT(*) FROM conversation_fact_aliases WHERE legacy_kind='EVENT' AND status='QUARANTINED'"
    ) if has_aliases else 0
    targets = count(
        "SELECT COUNT(*) FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
        "WHERE c.type='DIRECT'"
    ) if has_facts and has_conversations else 0
    groups = count(
        "SELECT COUNT(*) FROM conversation_events e JOIN conversations c ON c.id=e.conversation_id "
        "WHERE c.type='GROUP'"
    ) if has_facts and has_conversations else 0
    direct_conversations = count(
        "SELECT COUNT(*) FROM conversations WHERE type='DIRECT'"
    ) if has_conversations else 0
    return {
        "source_fingerprint": fingerprint,
        "in_scope_source_rows": total,
        "by_event_type": {
            event_type: sum(1 for row in rows if row["event_type"] == event_type)
            for event_type in MESSAGE_TYPES
        },
        "not_in_scope_by_event_type": {
            key: value for key, value in _all_event_types(conn).items() if key not in MESSAGE_TYPES
        },
        "aliases_migrated": migrated,
        "aliases_quarantined": quarantined,
        "alias_coverage": "complete" if migrated + quarantined == total else "INCOMPLETE",
        "direct_conversation_facts": targets,
        "group_conversation_facts": groups,
        "direct_conversations": direct_conversations,
        "sequence_reserved_to": _sequence_value(conn),
        "counts": {
            "events_total": count("SELECT COUNT(*) FROM events"),
            "events_messages": total,
            "conversation_events_total": count("SELECT COUNT(*) FROM conversation_events") if has_facts else 0,
        },
    }


def run(path: Path, *, apply: bool, backup: Path | None, allow_unbacked: bool,
        migration_id: str, batch_size: int, cutover: bool, expect_fingerprint: str | None) -> dict[str, Any]:
    if not path.exists():
        raise MigrationAborted(f"database does not exist: {path}")
    backup_path = backup
    if apply and not allow_unbacked:
        if backup_path is None:
            raise MigrationAborted("--apply requires --backup (or --allow-unbacked for a disposable copy)")
        if backup_path.exists() and backup_path.stat().st_size > 0:
            # Never overwrite an earlier backup: the pre-migration copy is the
            # primary rollback artifact.
            raise MigrationAborted(f"backup destination already exists, refusing to overwrite: {backup_path}")
        if backup_path.resolve() == path.resolve():
            raise MigrationAborted("--backup must not be the database itself")

    conn = _connect(path, readonly=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        rows = _source_rows(conn)
        fingerprint = source_fingerprint(conn)
        if expect_fingerprint and expect_fingerprint != fingerprint:
            raise MigrationAborted(
                "source fingerprint differs from the dry-run this --apply was authorised by"
            )
        before = _report(conn, rows, fingerprint)
        if not apply:
            return {"mode": "dry-run", "database": str(path), "before": before,
                    "would_apply": len(rows)}

        if backup_path is not None:
            # SQLite's online backup API, never a raw copy of a live WAL file.
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                conn.backup(backup_conn)
                check = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise MigrationAborted(f"backup integrity_check failed: {check}")
                fk = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk:
                    raise MigrationAborted("backup foreign_key_check reported violations")
            finally:
                backup_conn.close()

        limits = _assert_floor_contract(conn)
        _ensure_schema(conn)
        reserved = _reserve_sequence(conn, limits["max_legacy_message_id"])
        source_ids = {int(row["id"]) for row in rows}

        counts = {"migrated": 0, "skipped": 0, "skipped_quarantined": 0, "quarantined": 0}
        warnings: dict[str, int] = {}
        started = time.perf_counter()
        last_id = 0
        for chunk in _batch(rows, max(1, int(batch_size))):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # A source row that appeared after the fingerprint can only mean the
                # database was written during the migration window.
                highest = conn.execute(
                    f"SELECT COALESCE(MAX(id),0) FROM events WHERE event_type IN ({','.join('?' for _ in MESSAGE_TYPES)})",
                    MESSAGE_TYPES,
                ).fetchone()[0]
                if int(highest) > limits["max_legacy_message_id"]:
                    raise MigrationAborted(
                        "new Direct messages appeared during the backfill; stop writers and restart"
                    )
                for row in chunk:
                    _migrate_row(conn, row, source_ids=source_ids,
                                 counts=counts, warnings=warnings)
                last_id = int(chunk[-1]["id"])
                _checkpoint(conn, migration_id, fingerprint, last_id, counts, finished=False)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        _checkpoint(conn, migration_id, fingerprint, last_id, counts, finished=True)
        if cutover:
            conn.execute(
                "INSERT INTO conversation_fact_cutover(id,mode,migration_id,database_epoch,updated_at) "
                "VALUES(1,'UNIFIED',?,?,?) ON CONFLICT(id) DO UPDATE SET mode='UNIFIED',"
                "migration_id=excluded.migration_id,database_epoch=excluded.database_epoch,"
                "updated_at=excluded.updated_at",
                (migration_id, fingerprint, _utcnow()),
            )
        conn.execute(
            "INSERT INTO conversation_fact_migrations(migration_id,source_fingerprint,started_at,"
            "finished_at,last_legacy_id,counts_json,errors_json) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(migration_id) DO UPDATE SET counts_json=excluded.counts_json,"
            "errors_json=excluded.errors_json",
            (migration_id, fingerprint, _utcnow(), _utcnow(), last_id,
             json.dumps({**counts, "warnings": warnings}, sort_keys=True),
             json.dumps(warnings, sort_keys=True)),
        )
        after = _report(conn, rows, fingerprint)
        return {
            "mode": "apply",
            "database": str(path),
            "backup": str(backup_path) if backup_path is not None else None,
            "migration_id": migration_id,
            "fingerprint": fingerprint,
            "reserved_sequence": reserved,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "counts": counts,
            "warnings": warnings,
            "before": before,
            "after": after,
            "cutover_recorded": bool(cutover),
        }
    finally:
        conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Direct conversation facts into conversation_events.")
    parser.add_argument("--db", required=True, help="SQLite database path (use a copy unless this is the formal cutover)")
    parser.add_argument("--backup", help="online backup destination; required for --apply")
    parser.add_argument("--allow-unbacked", action="store_true",
                        help="permit --apply without a backup; only for a disposable copy")
    parser.add_argument("--migration-id", default=None, help="ledger id; defaults to a timestamp")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--apply", action="store_true", help="write; omitted means dry-run")
    parser.add_argument("--cutover", action="store_true",
                        help="record mode=UNIFIED in conversation_fact_cutover (formal step only)")
    parser.add_argument("--confirm", dest="expect_fingerprint", default=None,
                        help="exact source fingerprint the dry-run reported")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    migration_id = args.migration_id or f"conversation-facts-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        result = run(
            Path(args.db),
            apply=args.apply,
            backup=Path(args.backup) if args.backup else None,
            allow_unbacked=args.allow_unbacked,
            migration_id=migration_id,
            batch_size=args.batch_size,
            cutover=args.cutover,
            expect_fingerprint=args.expect_fingerprint,
        )
    except MigrationAborted as exc:
        print(f"MIGRATION ABORTED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"mode: {result['mode']}  database: {result['database']}")
        report = result.get("after") or result["before"]
        for key in ("in_scope_source_rows", "aliases_migrated", "aliases_quarantined",
                    "alias_coverage", "direct_conversation_facts", "group_conversation_facts",
                    "direct_conversations", "sequence_reserved_to", "counts"):
            print(f"  {key}: {report[key]}")
        print(f"  not_in_scope_by_event_type: {report['not_in_scope_by_event_type']}")
        if result["mode"] == "apply":
            print(f"  migration_id: {result['migration_id']}")
            print(f"  counts: {result['counts']}")
            print(f"  warnings: {result['warnings']}")
            print(f"  cutover_recorded: {result['cutover_recorded']}")
        else:
            print(f"  dry-run: nothing written (would migrate {result['would_apply']} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
