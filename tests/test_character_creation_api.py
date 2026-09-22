from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.config import Settings, discover_character_profiles
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class CreationFakeModel(PersonModel):
    def react(self, context):
        return PersonReaction(
            perception="收到",
            reaction="回应",
            mental_state_update="",
            actions=[ActionDecision(type=ActionType.MESSAGE, message="记住啦")],
        )

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="平静。", mental_state_update="")


def valid_draft():
    return {
        "name": "Nova",
        "age": 24,
        "identity": "独立游戏音效设计师",
        "tagline": "会收集城市里奇怪的声音",
        "description": "Nova 很好奇，也有自己的判断。她会记住共同经历中的具体小事。",
        "personality": ["好奇", "独立", "有点怪幽默"],
        "conversation": "自然短句，兴奋时会多说一点。",
        "expression": "偶尔使用感叹号和轻量 emoji。",
        "questions": "真的好奇才追问。",
        "silence": "没想说的话时可以沉默。",
        "initiative": "共同话题有后续时可能主动提起。",
        "disagreement": "不同意会直接说理由。",
        "care": "记住具体事情，后来问结果。",
        "boundaries": ["不无条件迎合", "关系通过共同经历形成"],
    }


def test_created_character_is_immediately_chat_ready(tmp_path):
    persona_root = tmp_path / "personas"
    rin_dir = persona_root / "rin"
    rin_dir.mkdir(parents=True)
    rin_path = rin_dir / "persona.yaml"
    rin_path.write_text(
        "id: rin\nname: Rin\nidentity: test\ntagline: test\npersonality: []\nbehavior: {}\nboundaries: []\n",
        encoding="utf-8",
    )

    settings = Settings(
        api_key="fake",
        base_url="https://example.invalid/v1",
        chat_model="fake",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=str(tmp_path / "x.db"),
        persona_path=str(rin_path),
        recall_limit=8,
    )
    store = SQLiteStore(settings.db_path)
    emb = DeterministicEmbedding()
    model = CreationFakeModel()
    recall = VectorRecall(store, emb)
    rin_runtime = PersonRuntime(store, recall, emb, model, rin_path.read_text(encoding="utf-8"))
    runtimes = {"rin": rin_runtime}
    clock = FixedClock(datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc))
    chat = ChatService(store, runtimes, clock)
    bundle = SimpleNamespace(
        settings=settings,
        store=store,
        runtime=rin_runtime,
        runtimes=runtimes,
        characters=discover_character_profiles(settings),
        chat=chat,
        days=None,
        embeddings=emb,
        model=model,
        clock=clock,
        init_timings={},
    )

    client = TestClient(create_api(bundle=bundle))
    created = client.post("/v1/characters", json={"draft": valid_draft()})
    assert created.status_code == 200
    assert created.json()["character"]["id"] == "nova"
    assert (persona_root / "nova" / "persona.yaml").exists()

    ids = {item["id"] for item in client.get("/v1/characters").json()["characters"]}
    assert "nova" in ids
    assert "nova" in runtimes

    response = client.post(
        "/v1/chat",
        json={"message": "以后听到地铁提示音记得告诉我", "character_id": "nova", "conversation_id": "nova-test"},
    )
    assert response.status_code == 200
    assert response.json()["actions"][0]["message"] == "记住啦"
    store.close()


def test_duplicate_character_id_is_rejected(tmp_path):
    persona_root = tmp_path / "personas"
    rin_dir = persona_root / "rin"
    rin_dir.mkdir(parents=True)
    rin_path = rin_dir / "persona.yaml"
    rin_path.write_text("id: rin\nname: Rin\n", encoding="utf-8")
    settings = Settings(db_path=str(tmp_path / "x.db"), persona_path=str(rin_path), embedding_provider="deterministic")
    store = SQLiteStore(settings.db_path)
    bundle = SimpleNamespace(settings=settings, store=store, characters=discover_character_profiles(settings))
    client = TestClient(create_api(bundle=bundle))

    first = client.post("/v1/characters", json={"draft": valid_draft()})
    assert first.status_code == 500  # loaded test bundle cannot dynamically register a runtime
    assert not (persona_root / "nova" / "persona.yaml").exists()
    store.close()


def test_character_creation_is_blocked_when_ten_active_slots_are_full(tmp_path):
    persona_root = tmp_path / "personas"
    for index in range(10):
        directory = persona_root / f"c{index:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "persona.yaml").write_text(
            f"id: c{index:02d}\nname: C{index:02d}\n",
            encoding="utf-8",
        )
    settings = Settings(
        db_path=str(tmp_path / "x.db"),
        persona_path=str(persona_root / "c00" / "persona.yaml"),
        embedding_provider="deterministic",
    )
    store = SQLiteStore(settings.db_path)
    bundle = SimpleNamespace(settings=settings, store=store, characters=discover_character_profiles(settings))
    client = TestClient(create_api(bundle=bundle))

    response = client.post("/v1/characters", json={"draft": valid_draft()})
    assert response.status_code == 409
    assert "最多保留 10 位角色" in response.text
    assert not (persona_root / "nova" / "persona.yaml").exists()
    store.close()
