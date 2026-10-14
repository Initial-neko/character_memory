from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import threading
import time

from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore, STORE_BUSY_TIMEOUT_SECONDS


def message(text: str) -> Event:
    return Event(
        character_id="rin",
        event_type=EventType.USER_MESSAGE,
        event_time=datetime(2026, 10, 13, tzinfo=timezone.utc),
        content=text,
    )


def test_core_store_uses_wal_and_bounded_busy_wait(tmp_path):
    store = SQLiteStore(tmp_path / "wal-mode.db")
    try:
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(STORE_BUSY_TIMEOUT_SECONDS * 1000)
        assert store.append_event(message("committed")).id
    finally:
        store.close()


def test_world_writer_commits_while_independent_reader_holds_snapshot(tmp_path):
    path = tmp_path / "shared-world.db"
    reader = SQLiteStore(path)
    writer = SQLiteStore(path)
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def hold_snapshot():
        try:
            with reader.transaction():
                assert reader.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
                entered.set()
                assert release.wait(timeout=3), "reader timed out"
                # Keep the original, consistent WAL snapshot until the transaction ends.
                assert reader.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        except Exception as error:
            errors.append(error)
            entered.set()

    thread = threading.Thread(target=hold_snapshot, daemon=True)
    try:
        thread.start()
        assert entered.wait(timeout=3)
        # DELETE journaling would block on the reader's pending transaction.
        # WAL must let this return before the snapshot is released.
        inserted = writer.append_event(message("世界里的新经历"))
        assert inserted.id
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not errors
        assert len(reader.list_events("rin")) == 1
    finally:
        release.set()
        thread.join(timeout=3)
        reader.close()
        writer.close()


def test_two_writers_serialize_and_preserve_both_events(tmp_path):
    path = tmp_path / "writers.db"
    first = SQLiteStore(path)
    second = SQLiteStore(path)
    attempting = threading.Event()
    completed = threading.Event()
    errors: list[Exception] = []

    def write_second():
        try:
            attempting.set()
            second.append_event(message("second"))
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=write_second, daemon=True)
    try:
        with first.transaction(immediate=True):
            first.append_event(message("first"))
            thread.start()
            assert attempting.wait(timeout=2)
            # The other independent SQLite writer must wait rather than fail.
            assert not completed.wait(timeout=0.2)
        assert completed.wait(timeout=3)
        thread.join(timeout=3)
        assert not errors
        assert [e.content for e in first.list_events("rin")] == ["first", "second"]
    finally:
        thread.join(timeout=3) if thread.ident is not None else None
        first.close()
        second.close()


def test_busy_writer_fails_boundedly_without_leaking_transaction(tmp_path):
    path = tmp_path / "blocked.db"
    holder = SQLiteStore(path)
    blocked = SQLiteStore(path)
    try:
        # Keep this test short while proving the configured timeout is honored.
        blocked.conn.execute("PRAGMA busy_timeout=100")
        with holder.transaction(immediate=True):
            started = time.monotonic()
            try:
                blocked.append_event(message("never committed"))
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
            else:
                raise AssertionError("a second writer committed through an exclusive writer lock")
            assert time.monotonic() - started < 2
            assert not blocked.conn.in_transaction
        assert blocked.list_events("rin") == []
        # The failed writer remains healthy and can run again after rollback.
        assert blocked.append_event(message("retry works")).id
    finally:
        holder.close()
        blocked.close()
