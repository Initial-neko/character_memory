from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore


def test_character_summary_exposes_latest_preview_and_proactive_source(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    user = store.append_event(Event(character_id="rin", event_type=EventType.USER_MESSAGE, event_time=now, content="下午再聊", metadata={"conversation_id": "x"}))
    source = store.append_event(Event(character_id="rin", event_type=EventType.PROACTIVE_INTENT, event_time=now, content="问问用户", metadata={"conversation_id": "x", "intent_id": 1}))
    reply = store.append_event(Event(character_id="rin", event_type=EventType.CHARACTER_MESSAGE, event_time=now, content="忙完了吗？", metadata={"conversation_id": "x", "action": "MESSAGE", "source_event_id": source.id, "source_event_type": EventType.PROACTIVE_INTENT.value}))

    settings = SimpleNamespace(
        chat_model="fake",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=str(tmp_path / "x.db"),
        base_url="fake",
        persona_path="personas/rin/persona.yaml",
        api_key="",
    )
    bundle = SimpleNamespace(settings=settings, store=store, characters=[{"id": "rin", "name": "Rin", "identity": "", "tagline": "", "persona_path": "personas/rin/persona.yaml"}])
    client = TestClient(create_api(bundle=bundle))

    summary = client.get("/v1/characters/summaries")
    assert summary.status_code == 200
    item = summary.json()["characters"][0]
    assert item["latest_assistant_message_id"] == reply.id
    assert item["latest_message"]["id"] == reply.id
    assert item["latest_message"]["content"] == "忙完了吗？"
    assert item["latest_message"]["proactive"] is True

    history = client.get("/v1/chat/history?character_id=rin").json()["messages"]
    proactive = next(message for message in history if message["id"] == reply.id)
    assert proactive["proactive"] is True
    assert proactive["source_event_type"] == EventType.PROACTIVE_INTENT.value
    assert user.id is not None
    store.close()
