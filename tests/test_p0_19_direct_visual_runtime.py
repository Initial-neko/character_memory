from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from character_memory.avatars import AvatarStore
from character_memory.config import Settings
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType
from character_memory.media import MediaStorage
from character_memory.runtime.context import compile_context
from character_memory.storage.sqlite import SQLiteStore
from character_memory.visual_generation import ImageGenerationResult
from character_memory.visual_runtime import DirectVisualRuntime


_PNG = b"\x89PNG\r\n\x1a\nvisual-runtime-test"


class FakeProvider:
    name = "agnes"
    model = "fake-agnes"
    supports_reference_images = True

    def __init__(self):
        self.requests = []

    def available(self):
        return True

    def generate(self, request):
        self.requests.append(request)
        return ImageGenerationResult(
            provider="agnes",
            model=self.model,
            payload=_PNG,
            mime_type="image/png",
        )


class FakePlannerModel:
    def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
        assert json_object is False
        assert "不返回 JSON" in messages[0]["content"]
        assert conversation_id.startswith("visual-plan:mika:")
        return "same person, natural casual image"


def _runtime(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    provider = FakeProvider()
    avatar_store = AvatarStore(tmp_path / "avatars")
    media_storage = MediaStorage(tmp_path / "media")
    settings = Settings(
        db_path=str(tmp_path / "db.sqlite"),
        image_generation_provider="agnes",
        agnes_api_key="configured-for-test",
    )
    access = SimpleNamespace(
        settings=settings,
        image_generation_providers={"agnes": provider},
        avatar_store=avatar_store,
        media_storage=media_storage,
    )
    service = DirectVisualRuntime(access)
    runtime = SimpleNamespace(
        store=store,
        model=FakePlannerModel(),
        persona="Mika has stable black hair and blue eyes.",
    )
    return service, runtime, provider, avatar_store, media_storage, store


def test_generate_image_action_requires_selfie_or_scene_intent():
    action = ActionDecision(
        type=ActionType.GENERATE_IMAGE,
        image_purpose="selfie",
        visual_intent="想自然地给对方看看现在的样子",
    )
    assert action.image_purpose == "SELFIE"
    assert action.visual_intent
    assert action.message is None

    with pytest.raises(ValueError, match="SELFIE or SCENE"):
        ActionDecision(type=ActionType.GENERATE_IMAGE, image_purpose="AVATAR", visual_intent="test")
    with pytest.raises(ValueError, match="visual_intent"):
        ActionDecision(type=ActionType.GENERATE_IMAGE, image_purpose="SCENE", visual_intent="")


def test_generate_image_contract_is_opt_in_and_default_context_is_group_safe():
    now = datetime.now().astimezone()
    event = Event(character_id="mika", event_type=EventType.USER_MESSAGE, event_time=now, content="给我看看你")
    direct = compile_context("persona", "", [], event, allow_generate_image=True)
    group_safe_default = compile_context("persona", "", [], event)

    assert "GENERATE_IMAGE" in direct
    assert "用户说“发张自拍/画给我看”" in direct
    assert "image_purpose" in direct
    assert "GENERATE_IMAGE" not in group_safe_default


def test_direct_selfie_uses_avatar_reference_and_persists_generated_image(tmp_path):
    service, runtime, provider, avatar_store, media_storage, store = _runtime(tmp_path)
    avatar_store.save_from_bytes(
        "mika",
        _PNG,
        "image/png",
        source="CHAT_ASSET",
        title="current avatar",
    )
    source = store.append_event(
        Event(
            character_id="mika",
            event_type=EventType.USER_MESSAGE,
            event_time=datetime.now().astimezone(),
            content="你愿意的话给我看看现在的样子",
            metadata={"conversation_id": "conv-1", "display_text": "你愿意的话给我看看现在的样子"},
        )
    )
    action = ActionDecision(
        type=ActionType.GENERATE_IMAGE,
        image_purpose="SELFIE",
        visual_intent="愿意分享一张现在在家的自然自拍",
    )

    generated = service.generate(runtime, source, action, still_current=lambda: True)

    assert generated is not None
    assert generated.metadata["action"] == ActionType.IMAGE.value
    assert generated.metadata["generation_purpose"] == "SELFIE"
    assert generated.metadata["provider"] == "agnes"
    assert generated.metadata["source_event_id"] == source.id
    assert generated.metadata["conversation_id"] == "conv-1"
    assert "prompt" not in generated.metadata
    assert len(provider.requests) == 1
    assert provider.requests[0].aspect_ratio == "3:4"
    assert len(provider.requests[0].reference_images) == 1
    assert provider.requests[0].reference_images[0].startswith("data:image/png;base64,")
    assert "reference image as the identity anchor" in provider.requests[0].prompt

    asset = store.get_media_asset(generated.metadata["media_id"])
    assert asset is not None
    assert asset.source == "GENERATED_SELFIE"
    assert media_storage.asset_path(asset).read_bytes() == _PNG

    avatar_store.close()
    store.close()


def test_scene_generation_does_not_force_avatar_reference(tmp_path):
    service, runtime, provider, avatar_store, _media_storage, store = _runtime(tmp_path)
    avatar_store.save_from_bytes("mika", _PNG, "image/png", source="CHAT_ASSET")
    source = store.append_event(
        Event(
            character_id="mika",
            event_type=EventType.USER_MESSAGE,
            event_time=datetime.now().astimezone(),
            content="刚才说的雨夜是什么感觉",
            metadata={"conversation_id": "conv-2"},
        )
    )
    action = ActionDecision(
        type=ActionType.GENERATE_IMAGE,
        image_purpose="SCENE",
        visual_intent="想把刚才描述的安静雨夜画给对方看",
    )
    generated = service.generate(runtime, source, action)
    assert generated is not None
    assert provider.requests[0].reference_images == []
    assert provider.requests[0].aspect_ratio == "4:3"
    assert generated.metadata["generation_purpose"] == "SCENE"
    avatar_store.close()
    store.close()


def test_slow_generation_is_discarded_when_newer_user_fact_supersedes_it(tmp_path):
    service, runtime, provider, avatar_store, _media_storage, store = _runtime(tmp_path)
    source = store.append_event(
        Event(
            character_id="mika",
            event_type=EventType.USER_MESSAGE,
            event_time=datetime.now().astimezone(),
            content="发张自拍？",
            metadata={"conversation_id": "conv-3"},
        )
    )
    action = ActionDecision(
        type=ActionType.GENERATE_IMAGE,
        image_purpose="SELFIE",
        visual_intent="本来想分享自拍",
    )

    generated = service.generate(runtime, source, action, still_current=lambda: False)
    assert generated is None
    assert len(provider.requests) == 1
    assert [event for event in store.list_chat_events("mika") if event.event_type == EventType.CHARACTER_MESSAGE] == []
    assert store.conn.execute("SELECT COUNT(*) AS n FROM media_assets").fetchone()["n"] == 0
    avatar_store.close()
    store.close()


def test_group_service_source_does_not_enable_generated_image_contract():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "character_memory" / "application" / "group_conversation_service.py").read_text(encoding="utf-8")
    assert "allow_generate_image=True" not in source
    assert "generate_direct_visual_action" not in source
