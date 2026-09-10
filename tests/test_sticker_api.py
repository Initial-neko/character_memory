from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore


def _bundle(store):
    settings = SimpleNamespace(
        chat_model="deepseek-flash",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=store.path,
        base_url="fake",
        persona_path="personas/rin/persona.yaml",
        api_key="",
    )
    return SimpleNamespace(
        settings=settings,
        store=store,
        characters=[{"id": "rin", "name": "Rin", "identity": "", "tagline": "", "persona_path": "personas/rin/persona.yaml"}],
    )


def test_sticker_catalog_and_asset_are_available(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    client = TestClient(create_api(bundle=_bundle(store)))

    response = client.get("/v1/stickers?character_id=rin")
    assert response.status_code == 200
    stickers = response.json()["stickers"]
    assert len(stickers) >= 8
    selected = next(item for item in stickers if item["id"] == "round_cat_happy")
    asset = client.get(selected["url"])
    assert asset.status_code == 200
    assert b"<svg" in asset.content
    store.close()


def test_history_exposes_sticker_without_losing_provenance(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    source = store.append_event(Event(character_id="rin", event_type=EventType.PROACTIVE_INTENT, event_time=now, content="发个表情", metadata={"conversation_id": "x"}))
    event = store.append_event(
        Event(
            character_id="rin",
            event_type=EventType.CHARACTER_MESSAGE,
            event_time=now,
            content="[表情包：圆猫·开心]",
            metadata={
                "action": "STICKER",
                "sticker_id": "round_cat_happy",
                "sticker_label": "圆猫·开心",
                "source_event_id": source.id,
                "source_event_type": EventType.PROACTIVE_INTENT.value,
                "conversation_id": "x",
            },
        )
    )
    client = TestClient(create_api(bundle=_bundle(store)))

    messages = client.get("/v1/chat/history?character_id=rin").json()["messages"]
    message = next(item for item in messages if item["id"] == event.id)
    assert message["sticker_id"] == "round_cat_happy"
    assert message["sticker"]["url"].endswith("/round_cat_happy/asset")
    assert message["preview"] == "[表情包] 圆猫·开心"
    assert message["proactive"] is True
    store.close()
