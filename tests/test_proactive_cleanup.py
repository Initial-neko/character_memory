"""Retiring the already-due Intent backlog.

The cleanup is deliberately conservative: it only moves PENDING rows to
EXPIRED, never deletes, and does nothing at all unless dry_run=False.
"""

from datetime import datetime, timedelta, timezone

from character_memory.domain.models import ActionType
from character_memory.storage.sqlite import SQLiteStore


def _intent(store, character_id, now, content, *, earliest_delta=0, source_event_id=None):
    return store.add_intent(
        character_id,
        content,
        ActionType.PROACTIVE_MESSAGE.value,
        now - timedelta(hours=2),
        now + timedelta(hours=earliest_delta),
        now + timedelta(hours=6),
        source_event_id=source_event_id,
    )


def _status(store, character_id, intent_id):
    return next(row["status"] for row in store.list_intents(character_id) if row["id"] == intent_id)


def test_cleanup_is_a_dry_run_by_default(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    intent_id = _intent(store, "momo", now, "明早看叶子")

    report = store.expire_due_backlog_intents(now)

    assert report["dry_run"] is True
    assert report["candidates"] == 1
    assert report["expired"] == 0
    assert report["by_character"] == {"momo": 1}
    assert _status(store, "momo", intent_id) == "PENDING"
    store.close()


def test_cleanup_expires_only_the_due_rows(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    due = _intent(store, "momo", now, "已经到期的")
    scheduled = _intent(store, "momo", now, "还没到期的", earliest_delta=3)

    report = store.expire_due_backlog_intents(now, dry_run=False)

    assert report["dry_run"] is False
    assert report["expired"] == 1
    assert _status(store, "momo", due) == "EXPIRED"
    assert _status(store, "momo", scheduled) == "PENDING"
    store.close()


def test_cleanup_is_idempotent_and_keeps_every_row(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    intent_id = _intent(store, "momo", now, "明早看叶子", source_event_id=42)

    store.expire_due_backlog_intents(now, dry_run=False)
    second = store.expire_due_backlog_intents(now, dry_run=False)

    assert second["candidates"] == 0
    assert second["expired"] == 0
    rows = store.list_intents("momo")
    assert len(rows) == 1
    assert rows[0]["id"] == intent_id
    assert rows[0]["source_event_id"] == 42
    assert rows[0]["content"] == "明早看叶子"
    store.close()


def test_cleanup_can_target_one_character(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    _intent(store, "momo", now, "momo 的意图")
    rei = _intent(store, "rei", now, "rei 的意图")

    report = store.expire_due_backlog_intents(now, character_id="momo", dry_run=False)

    assert report["by_character"] == {"momo": 1}
    assert _status(store, "rei", rei) == "PENDING"
    store.close()


def test_all_pending_expires_scheduled_rows_too(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    scheduled = _intent(store, "momo", now, "还没到期的", earliest_delta=3)

    report = store.expire_due_backlog_intents(now, all_pending=True, dry_run=False)

    assert report["expired"] == 1
    assert _status(store, "momo", scheduled) == "EXPIRED"
    store.close()


def test_due_before_narrows_the_cleanup_boundary(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    older = _intent(store, "momo", now, "很早的意图", earliest_delta=-5)
    recent = _intent(store, "momo", now, "刚过期的意图", earliest_delta=-1)

    report = store.expire_due_backlog_intents(now, due_before=now - timedelta(hours=3), dry_run=False)

    assert report["expired"] == 1
    assert _status(store, "momo", older) == "EXPIRED"
    assert _status(store, "momo", recent) == "PENDING"
    store.close()
