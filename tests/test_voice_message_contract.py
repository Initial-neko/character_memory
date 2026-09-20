import pytest

from character_memory.domain.models import ActionDecision, ActionType, EXPRESSIVE_ACTIONS


def test_voice_message_is_an_action_type():
    assert ActionType.VOICE_MESSAGE.value == "VOICE_MESSAGE"


def test_voice_message_keeps_its_text_and_drops_visual_fields():
    decision = ActionDecision(
        type="VOICE_MESSAGE",
        message="  晚上好呀  ",
        sticker_id="sticker-1",
        image_id="image-1",
    )

    assert decision.type == ActionType.VOICE_MESSAGE
    # Stripped, not erased: the text is what the bubble shows when expanded.
    assert decision.message == "晚上好呀"
    assert decision.sticker_id is None
    assert decision.image_id is None


def test_voice_message_requires_a_non_empty_message():
    with pytest.raises(ValueError, match="VOICE_MESSAGE requires a non-empty message"):
        ActionDecision(type="VOICE_MESSAGE", message="   ")


def test_voice_message_is_expressive():
    assert ActionType.VOICE_MESSAGE in EXPRESSIVE_ACTIONS


def test_both_chat_modes_consult_the_same_action_set():
    """Identity, not equality: two equal sets would drift apart on the next edit,
    and the drift is silent -- the action just stops producing a message."""
    from character_memory.application import group_conversation_service
    from character_memory.runtime import person_runtime

    assert person_runtime.EXPRESSIVE_ACTIONS is EXPRESSIVE_ACTIONS
    assert group_conversation_service.EXPRESSIVE_ACTIONS is EXPRESSIVE_ACTIONS
