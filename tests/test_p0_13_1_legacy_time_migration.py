from __future__ import annotations

from datetime import datetime
import sqlite3

import pytest

from character_memory.storage.sqlite import SQLiteStore


def _create_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE mental_states(
            character_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source_event_id INTEGER
        );
        CREATE TABLE world_states(
            character_id TEXT PRIMARY KEY,
            current_time TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?)",
        ("rin", "USER_MESSAGE", "10:49:43", "corrupt-time-only", "{}"),
    )
    conn.execute(
        "INSERT INTO events(character_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?)",
        ("rin", "USER_MESSAGE", "2026-09-11T10:49:43+08:00", "valid-datetime", "{}"),
    )
    conn.execute(
        "INSERT INTO mental_states(character_id,content,updated_at,source_event_id) VALUES(?,?,?,?)",
        ("rin", "unknown-date-state", "10:49:43", None),
    )
    conn.execute(
        "INSERT INTO world_states(character_id,current_time) VALUES(?,?)",
        ("rin", "10:49:43"),
    )
    conn.commit()
    conn.close()


def test_invalid_legacy_time_only_values_are_preserved_but_quarantined(tmp_path, caplog):
    path = tmp_path / "legacy.db"
    _create_legacy_db(path)

    with caplog.at_level("WARNING", logger="character_memory.storage"):
        store = SQLiteStore(path)
        try:
            rows = store.conn.execute(
                "SELECT content,event_time,event_time_epoch FROM events ORDER BY id"
            ).fetchall()
            assert rows[0]["event_time"] == "10:49:43"
            assert rows[0]["event_time_epoch"] is None
            assert rows[1]["event_time_epoch"] is not None

            # Unknown-date rows must not enter temporal context or be invented as a date.
            assert [event.content for event in store.list_events("rin")] == ["valid-datetime"]
            assert store.get_mental_state(
                "rin",
                at=datetime.fromisoformat("2026-09-11T12:00:00+08:00"),
            ) == ""
            assert store.get_world_time("rin") is None

            world = store.conn.execute(
                "SELECT current_time,current_time_epoch FROM world_states WHERE character_id='rin'"
            ).fetchone()
            assert world["current_time"] == "10:49:43"
            assert world["current_time_epoch"] is None

            history_count = store.conn.execute(
                "SELECT COUNT(*) AS n FROM mental_state_history WHERE character_id='rin'"
            ).fetchone()["n"]
            assert history_count == 0
        finally:
            store.close()

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "storage.time_migration skipped_invalid table=events column=event_time" in text
    assert "value='10:49:43'" in text
    assert "preserved_raw=true" in text


def test_webui_starts_with_legacy_time_only_rows(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette")
    from fastapi.testclient import TestClient

    from character_memory.api import create_api

    db_path = tmp_path / "legacy-web.db"
    _create_legacy_db(db_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_key: ''\n"
        "embedding_provider: deterministic\n"
        "embedding_model: deterministic\n"
        f"db_path: '{db_path.as_posix()}'\n"
        "persona_path: personas/rin/persona.yaml\n",
        encoding="utf-8",
    )

    app = create_api(str(config))
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["web"] == "ready"

    history = client.get("/v1/chat/history?character_id=rin")
    assert history.status_code == 200
    assert "corrupt-time-only" not in history.text
    assert "valid-datetime" in history.text
