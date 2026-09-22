from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest

from character_memory.encounter import EncounterScheduler, EncounterService
from character_memory.encounter_store import EncounterRepository
from character_memory.storage.sqlite import SQLiteStore


def draft():
    return {
        "name": "Mika",
        "age": 23,
        "identity": "夜间电器维修店的店员",
        "tagline": "会把坏掉的小东西拆开看看",
        "description": "Mika 习惯在夜里修理旧电子设备，观察细，熟悉以后会突然讲很多冷门零件。",
        "personality": ["安静", "好奇", "有自己的判断"],
        "conversation": "自然短句。",
        "expression": "很少刷表情。",
        "questions": "真的好奇才问。",
        "silence": "没话时可以沉默。",
        "initiative": "遇到旧设备会主动分享。",
        "disagreement": "不同意会直接说理由。",
        "care": "记住具体小事。",
        "boundaries": ["不迎合", "关系慢慢形成"],
    }


def test_encounter_candidate_pool_and_trial_messages_are_durable(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)

    candidate = repo.create_candidate(
        source_type="WEB",
        draft=draft(),
        encounter_hook="在一篇旧电器维修记录里得到的灵感。",
        opening_message="这个接口你还要用吗？我刚拆下来。",
        source_query="repair cafe vintage electronics hobby story",
        source_urls=["https://example.org/repair"],
        source_domains=["example.org"],
        now=now,
    )
    assert candidate["status"] == "NEW"
    assert candidate["source_type"] == "WEB"
    assert repo.pending_count() == 1

    repo.append_message(candidate["id"], "USER", "你修的是什么？", now)
    repo.append_message(candidate["id"], "CHARACTER", "一台很老的随身听。", now)
    assert [item["role"] for item in repo.list_messages(candidate["id"])] == ["USER", "CHARACTER"]

    repo.set_status(candidate["id"], "CHATTING", now)
    assert repo.get_candidate(candidate["id"])["status"] == "CHATTING"
    store.close()


def test_full_chat_list_does_not_close_or_delete_encounter(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    candidate = repo.create_candidate(
        source_type="GENERATED",
        draft=draft(),
        encounter_hook="偶然遇见。",
        opening_message="……你也是来找零件的吗？",
        now=now,
    )

    def full_creator(*_args, **_kwargs):
        raise ValueError("角色已达到容量上限：当前 20 位，本次新增 1 位，最多 20 位。")

    access = SimpleNamespace(
        create_character_from_draft=full_creator,
        store=lambda: store,
    )
    service = EncounterService(access, repo)
    with pytest.raises(ValueError, match="最多 20 位"):
        service.accept(candidate["id"], now=now)

    # The candidate remains available for trial chat/dismissal instead of being
    # destroyed merely because the formal chat list is full.
    assert repo.get_candidate(candidate["id"])["status"] == "NEW"
    store.close()


def test_scheduler_skips_creation_when_pending_pool_is_full(tmp_path):
    store = SQLiteStore(str(tmp_path / "x.db"))
    repo = EncounterRepository(store)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    repo.create_candidate(
        source_type="GENERATED",
        draft=draft(),
        encounter_hook="已有候选。",
        opening_message="你好。",
        now=now,
    )
    settings = SimpleNamespace(
        api_key="fake",
        encounter_enabled=True,
        encounter_interval_minutes=30,
        encounter_poll_seconds=60,
        encounter_web_probability=0.5,
        encounter_max_pending=1,
    )
    scheduler = EncounterScheduler(SimpleNamespace(settings=settings), repo)
    scheduler.force_due(now=now)
    result = scheduler.run_once(now=now)
    assert result[0]["status"] == "SKIPPED_PENDING"
    assert repo.pending_count() == 1
    store.close()


def test_encounter_ui_and_server_wiring_exist():
    root = Path(__file__).resolve().parents[1]
    server = (root / "src" / "character_memory" / "server.py").read_text(encoding="utf-8")
    index = (root / "src" / "character_memory" / "web" / "index.html").read_text(encoding="utf-8")
    script = (root / "src" / "character_memory" / "web" / "encounter.js").read_text(encoding="utf-8")

    assert "attach_encounter_routes" in server
    assert '/static/encounter.js' in index
    assert '/static/encounter.css' in index
    for token in [
        "/v1/encounters?limit=3",
        "data-encounter-chat",
        "data-encounter-accept",
        "data-encounter-dismiss",
        "active_character_soft_limit",
    ]:
        assert token in script
