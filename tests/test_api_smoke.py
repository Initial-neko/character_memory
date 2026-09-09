from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class ApiFakeModel(PersonModel):
    def react(self, context):
        return PersonReaction(perception="收到", reaction="回应", mental_state_update="平静", action=ActionDecision(type=ActionType.REPLY, reason="测试", message="你好"))

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="平静。", mental_state_update="平静")


def test_html_and_chat_share_same_application_service(tmp_path):
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, ApiFakeModel(), "persona")
    clock = FixedClock(datetime(2026, 9, 7, 21, 30, tzinfo=timezone.utc))
    service = ChatService(store, runtime, clock)
    settings = SimpleNamespace(chat_model="fake", embedding_provider="deterministic", embedding_model="deterministic", db_path=str(tmp_path / "x.db"), base_url="fake")
    bundle = SimpleNamespace(settings=settings, chat=service, store=store, clock=clock, runtime=runtime, days=None)
    app = create_api(bundle=bundle)
    client = TestClient(app)

    index = client.get("/")
    assert index.status_code == 200
    assert "Character Memory" in index.text

    response = client.post("/v1/chat", json={"message": "你好", "character_id": "rin", "conversation_id": "browser-test"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"]["message"] == "你好"
    assert body["timings"]["runtime_total_ms"] >= 0
    assert body["timings"]["api_total_ms"] >= body["timings"]["runtime_total_ms"]

    history = client.get("/v1/chat/history").json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    assert history["messages"][0]["has_trace"] is True
    source_event_id = history["messages"][0]["source_event_id"]
    trace = client.get(f"/v1/traces/{source_event_id}")
    assert trace.status_code == 200
    trace_body = trace.json()
    assert trace_body["conversation_id"] == "browser-test"
    assert trace_body["timings"]["model_ms"] >= 0
    store.close()
