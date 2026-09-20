from __future__ import annotations

from character_memory.storage.sqlite import SQLiteStore


EXPECTED_CORE_MIGRATIONS = [
    "core/001-intent-source-event",
    "core/002-epoch-time-keys",
    "core/003-mental-state-history",
    "core/004-runtime-trace-extraction",
    "core/005-indexes",
    "core/006-memory-candidate-indexes",
]


def test_core_schema_migrations_are_recorded_once(tmp_path):
    path = tmp_path / "versioned.db"

    first = SQLiteStore(path)
    try:
        assert first.list_schema_migrations() == EXPECTED_CORE_MIGRATIONS
        applied_first = {
            row["name"]: row["applied_at"]
            for row in first.conn.execute("SELECT name,applied_at FROM schema_migrations").fetchall()
        }
    finally:
        first.close()

    second = SQLiteStore(path)
    try:
        assert second.list_schema_migrations() == EXPECTED_CORE_MIGRATIONS
        applied_second = {
            row["name"]: row["applied_at"]
            for row in second.conn.execute("SELECT name,applied_at FROM schema_migrations").fetchall()
        }
        assert applied_second == applied_first
    finally:
        second.close()


def test_schema_migration_callback_runs_once(tmp_path):
    store = SQLiteStore(tmp_path / "feature.db")
    calls = []
    try:
        result = store.apply_schema_migration("test/001", lambda: calls.append("run") or 7)
        repeated = store.apply_schema_migration("test/001", lambda: calls.append("again") or 9)
        assert result == 7
        assert repeated is None
        assert calls == ["run"]
        assert "test/001" in store.list_schema_migrations()
    finally:
        store.close()
