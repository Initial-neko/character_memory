from datetime import datetime, timedelta, timezone
import sqlite3

from character_memory.config import Settings
from character_memory.storage.sqlite import SQLiteStore


def test_default_model_is_deepseek_v41_flash():
    assert Settings().chat_model == "deepseek-flash"


def test_intent_records_source_event_id(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    intent_id = store.add_intent(
        "rin",
        "晚上问问用户结果",
        "PROACTIVE_MESSAGE",
        now,
        now + timedelta(hours=1),
        now + timedelta(hours=6),
        source_event_id=42,
    )
    row = next(row for row in store.list_intents("rin") if row["id"] == intent_id)
    assert row["source_event_id"] == 42
    store.close()


def test_existing_intent_table_is_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE intents(id INTEGER PRIMARY KEY AUTOINCREMENT, character_id TEXT NOT NULL, content TEXT NOT NULL, preferred_action TEXT NOT NULL, created_at TEXT NOT NULL, earliest_at TEXT, expires_at TEXT, status TEXT NOT NULL DEFAULT 'PENDING', reason TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(intents)").fetchall()}
    assert "source_event_id" in columns
    store.close()


def test_sticker_module_and_default_pack_are_packaged_and_loaded():
    from pathlib import Path
    import character_memory

    root = Path(character_memory.__file__).parent
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "/static/stickers.js" in index
    assert "/static/p0_7.js" not in index
    assert "/static/p0_7.css" in index
    assert (root / "web" / "stickers" / "default" / "manifest.yaml").is_file()
