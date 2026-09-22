from pathlib import Path
from types import SimpleNamespace

from character_memory.application.action_materialization import materialize_expressive_action
from character_memory.domain.models import ActionDecision, ActionType
from character_memory.message_projection import common_chat_message_fields


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "character_memory"
WEB = SRC / "web"


class _Catalog:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def test_direct_and_group_share_one_action_materializer_contract():
    stickers = _Catalog(
        {
            "wave": SimpleNamespace(
                id="wave",
                label="挥手",
                tags=["问候"],
                description="",
            )
        }
    )
    images = _Catalog({"room": SimpleNamespace(id="room", label="房间")})

    message = materialize_expressive_action(
        ActionDecision(type=ActionType.MESSAGE, message="你好"),
        action_index=0,
        base_metadata={"channel": "test"},
    )
    voice = materialize_expressive_action(
        ActionDecision(type=ActionType.VOICE_MESSAGE, message="听我说"),
        action_index=1,
    )
    sticker = materialize_expressive_action(
        ActionDecision(type=ActionType.STICKER, sticker_id="wave"),
        action_index=2,
        sticker_catalog=stickers,
    )
    image = materialize_expressive_action(
        ActionDecision(type=ActionType.IMAGE, image_id="room"),
        action_index=3,
        image_catalog=images,
    )

    assert message.content == "你好"
    assert message.metadata["action"] == "MESSAGE"
    assert message.metadata["channel"] == "test"
    assert voice.content == "听我说"
    assert voice.metadata["voice_status"] == "pending"
    assert sticker.content == "[表情包：挥手]"
    assert sticker.metadata["sticker_id"] == "wave"
    assert image.content == "[图片：房间]"
    assert image.metadata["image_id"] == "room"


def test_direct_and_group_share_resource_projection_fields():
    metadata = {
        "action": "VOICE_MESSAGE",
        "action_index": 2,
        "voice_status": "ready",
        "voice_media_id": "voice-1",
        "voice_duration_ms": 1234,
        "voice_error": None,
        "sticker_id": None,
        "image_id": None,
        "media_id": None,
    }
    projected = common_chat_message_fields(metadata)
    assert projected["action"] == "VOICE_MESSAGE"
    assert projected["action_index"] == 2
    assert projected["voice_status"] == "ready"
    assert projected["voice_media_id"] == "voice-1"
    assert projected["voice_duration_ms"] == 1234


def test_direct_and_group_call_shared_reaction_and_message_layers():
    direct = (SRC / "runtime" / "person_runtime.py").read_text(encoding="utf-8")
    group = (SRC / "application" / "group_conversation_service.py").read_text(encoding="utf-8")

    for source in (direct, group):
        assert "evaluate_reaction(" in source
        assert "materialize_expressive_action(" in source
        assert "voice_pending_fields" not in source

    assert "compile_context(" not in group
    assert "GroupConversationService._react_member =" not in (
        SRC / "group_autonomous_visual.py"
    ).read_text(encoding="utf-8")


def test_direct_and_group_share_message_content_renderer():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    direct = (WEB / "app.js").read_text(encoding="utf-8")
    group = (WEB / "groups.js").read_text(encoding="utf-8")
    common = (WEB / "message_content.js").read_text(encoding="utf-8")

    assert "/static/message_content.js" in index
    assert index.index("/static/app.js") < index.index("/static/message_content.js") < index.index("/static/groups.js")
    assert "CM.messageContentHtml(message)" in direct
    assert "CM.messageContentHtml(message)" in group
    assert "CM.bindMessageContent(row)" in direct
    assert "CM.bindMessageContent(row)" in group
    assert "STICKER" in common
    assert "IMAGE" in common
    assert "VOICE_MESSAGE" in common
