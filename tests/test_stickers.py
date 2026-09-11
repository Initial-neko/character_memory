from datetime import datetime, timezone

import pytest

from character_memory.application.chat_service import ChatService
from character_memory.application.clock import FixedClock
from character_memory.domain.models import ActionDecision, ActionType, DailyLifePlan, DiaryResult, EventType, PersonReaction
from character_memory.llm.client import PersonModel
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.stickers import load_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


class StickerFakeModel(PersonModel):
    def __init__(self, sticker_id="round_cat_happy"):
        self.sticker_id = sticker_id
        self.last_context = ""

    def react(self, context):
        self.last_context = context
        return PersonReaction(actions=[ActionDecision(type=ActionType.STICKER, sticker_id=self.sticker_id)])

    def plan_day(self, context):
        return DailyLifePlan()

    def write_diary(self, context):
        return DiaryResult(diary="", mental_state_update="")


def test_sticker_action_requires_catalog_id():
    with pytest.raises(ValueError):
        ActionDecision(type=ActionType.STICKER)
    action = ActionDecision(type=ActionType.STICKER, sticker_id="round_cat_happy")
    assert action.message is None
    assert action.sticker_id == "round_cat_happy"


def test_default_sticker_catalog_has_real_assets(tmp_path):
    catalog = load_sticker_catalog(tmp_path / "persona.yaml")
    assert catalog.source == "default"
    assert len(catalog.stickers) >= 8
    assert catalog.get("round_duck_speechless") is not None
    assert catalog.asset_path("round_cat_happy").is_file()
    assert "round_cat_happy" in catalog.prompt_text()


def test_runtime_persists_valid_sticker_and_rejects_outside_working_set(tmp_path):
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    catalog = load_sticker_catalog(tmp_path / "persona.yaml")
    model = StickerFakeModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona", catalog)
    service = ChatService(store, {"momo": runtime}, FixedClock(now))

    out = service.send("好耶", character_id="momo", conversation_id="x", at=now)
    assert out.reaction.actions[0].type == ActionType.STICKER
    assert "# Available Stickers" in model.last_context
    messages = store.list_events("momo", event_type=EventType.CHARACTER_MESSAGE.value)
    assert messages[-1].metadata["sticker_id"] == "round_cat_happy"
    assert messages[-1].metadata["sticker_label"] == "圆猫·开心"

    model.sticker_id = "invented_sticker"
    out = service.send("再来一个", character_id="momo", conversation_id="x", at=now)
    assert out.reaction.actions == []
    trace = store.get_runtime_trace(out.event.id)
    assert trace["sticker_decisions"][0]["decision"] == "DROP_NOT_RETRIEVED_STICKER"
    store.close()


def test_user_sticker_is_semantic_for_model_but_display_text_stays_clean(tmp_path):
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "x.db")
    emb = DeterministicEmbedding()
    catalog = load_sticker_catalog(tmp_path / "persona.yaml")
    model = StickerFakeModel()
    runtime = PersonRuntime(store, VectorRecall(store, emb), emb, model, "persona", catalog)
    service = ChatService(store, {"momo": runtime}, FixedClock(now))
    sticker = catalog.get("round_duck_shock").model_dump(mode="json")

    service.send("", character_id="momo", conversation_id="x", at=now, sticker=sticker)
    user = store.list_events("momo", event_type=EventType.USER_MESSAGE.value)[-1]
    assert "用户发送表情包" in user.content
    assert user.metadata["display_text"] == ""
    assert user.metadata["sticker_id"] == "round_duck_shock"
    store.close()
