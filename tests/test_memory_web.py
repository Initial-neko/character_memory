from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.domain.models import Event, EventType, Memory
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory_web import attach_memory_routes
from character_memory.storage.sqlite import SQLiteStore


def test_memory_governance_http_round_trip(tmp_path):
    store = SQLiteStore(tmp_path / "memory-web.db")
    embeddings = DeterministicEmbedding()
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    source = store.append_event(
        Event(
            character_id="momo",
            event_type=EventType.USER_MESSAGE,
            event_time=now,
            content="我其实更喜欢绿茶。",
        )
    )
    memory = store.add_memory(
        Memory(
            character_id="momo",
            content="用户喜欢红茶",
            memory_type="USER",
            event_time=now,
            importance=0.6,
            source_event_id=source.id,
            embedding=embeddings.embed("用户喜欢红茶"),
        )
    )

    app = FastAPI()
    app.state.character_memory = SimpleNamespace(
        store=lambda: store,
        character_profiles=lambda: [{"id": "momo", "name": "Momo"}],
        require_bundle=lambda: SimpleNamespace(embeddings=embeddings),
    )
    attach_memory_routes(app)
    client = TestClient(app)

    listed = client.get("/v1/characters/momo/memories").json()["memories"]
    assert listed[0]["id"] == memory.id
    assert listed[0]["source"]["event_id"] == source.id
    assert listed[0]["source"]["content"] == "我其实更喜欢绿茶。"

    pinned = client.post(
        f"/v1/characters/momo/memories/{memory.id}/pin",
        json={"pinned": True},
    )
    assert pinned.status_code == 200
    assert pinned.json()["memory"]["pinned"] is True

    corrected = client.post(
        f"/v1/characters/momo/memories/{memory.id}/correct",
        json={"content": "用户不喜欢红茶，更喜欢绿茶"},
    )
    assert corrected.status_code == 200
    payload = corrected.json()
    assert payload["old_memory"]["active"] is False
    assert payload["old_memory"]["superseded_by"] == payload["memory"]["id"]
    assert payload["memory"]["pinned"] is True
    assert payload["memory"]["metadata"]["corrects_memory_id"] == memory.id

    forgotten = client.post(
        f"/v1/characters/momo/memories/{payload['memory']['id']}/active",
        json={"active": False},
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["memory"]["active"] is False
    store.close()
