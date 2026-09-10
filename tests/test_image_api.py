import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


class VisionFakeModel(PersonModel):
    def __init__(self):
        self.images = []

    def react(self, context):
        return PersonReaction(actions=[ActionDecision(type=ActionType.MESSAGE, message="普通回复")])

    def react_with_images_for_session(self, context, image_data_urls, session_id):
        self.images.append({"context": context, "urls": list(image_data_urls), "session_id": session_id})
        return PersonReaction(
            perception="用户给我看了一张图片",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="我看到了。")],
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def _bundle(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    model = VisionFakeModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona")
    clock = FixedClock(datetime(2026, 9, 10, 12, tzinfo=timezone.utc))
    chat = ChatService(store, runtime, clock)
    settings = SimpleNamespace(
        chat_model="deepseek-flash",
        vision_model="deepseek-v4-flash-vision-exp",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=store.path,
        media_dir=str(tmp_path / "media"),
        media_max_bytes=8 * 1024 * 1024,
        base_url="fake",
        persona_path="personas/rin/persona.yaml",
        api_key="",
    )
    bundle = SimpleNamespace(
        settings=settings,
        chat=chat,
        store=store,
        clock=clock,
        runtime=runtime,
        model=model,
        characters=[{"id": "rin", "name": "Rin", "identity": "", "tagline": "", "persona_path": "personas/rin/persona.yaml"}],
    )
    return bundle, model, store


def test_image_chat_persists_asset_and_sends_real_image_to_vision_runtime(tmp_path):
    bundle, model, store = _bundle(tmp_path)
    client = TestClient(create_api(bundle=bundle))
    data_url = f"data:image/png;base64,{base64.b64encode(PNG_1X1).decode('ascii')}"

    response = client.post(
        "/v1/chat",
        json={
            "message": "你看这个",
            "character_id": "rin",
            "conversation_id": "vision-test",
            "image": {"filename": "tiny.png", "data_url": data_url},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["actions"][0]["message"] == "我看到了。"
    assert body["input_image"]["label"] == "tiny.png"
    assert model.images[-1]["urls"] == [data_url]
    assert model.images[-1]["session_id"] == "vision-test"

    user_event = store.list_events("rin", event_type=EventType.USER_MESSAGE.value)[-1]
    assert "base64" not in user_event.content
    assert "iVBOR" not in str(user_event.metadata)
    assert user_event.metadata["media_id"] == body["input_image"]["id"]

    history = client.get("/v1/chat/history?character_id=rin").json()["messages"]
    user_message = history[0]
    assert user_message["content"] == "你看这个"
    assert user_message["image"]["label"] == "tiny.png"
    assert user_message["preview"] == "你看这个 [图片] tiny.png"

    asset = client.get(user_message["image"]["url"])
    assert asset.status_code == 200
    assert asset.content == PNG_1X1
    store.close()
