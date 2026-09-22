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


def test_the_expressive_set_holds_exactly_these_actions():
    """Membership, not just VOICE_MESSAGE's presence.

    Every other assertion here is one-sided, so widening the set -- adding
    NO_REPLY, say -- would quietly open all four action gates at once and the
    suite would stay green. A symmetric difference names the offender instead
    of dumping the whole set.
    """
    expected = {
        ActionType.REPLY,
        ActionType.MINIMAL_RESPONSE,
        ActionType.PROACTIVE_MESSAGE,
        ActionType.MESSAGE,
        ActionType.VOICE_MESSAGE,
        ActionType.EMOJI,
        ActionType.STICKER,
        ActionType.IMAGE,
    }

    assert EXPRESSIVE_ACTIONS ^ expected == set()


def test_every_action_gate_consults_the_shared_set():
    """Visible-message admission is centralized in the shared materializer.

    Direct and Group no longer own separate action gates; proactive/eval still
    consult the same domain set for their narrower orchestration paths.
    """
    from character_memory.application import action_materialization, proactive_service
    from character_memory.eval import runner as eval_runner

    for module in (action_materialization, proactive_service, eval_runner):
        assert module.EXPRESSIVE_ACTIONS is EXPRESSIVE_ACTIONS, module.__name__


def test_the_intent_ticker_counts_a_voice_reply_as_executed():
    """life/ticker.py keeps a narrower set on purpose -- it excludes STICKER and
    IMAGE, and this task must not widen that. But a voice reply does execute the
    intent, so VOICE_MESSAGE belongs there or the intent is logged as dropped."""
    from character_memory.life import ticker

    assert ActionType.VOICE_MESSAGE in ticker._INTENT_EXECUTED_ACTIONS
    assert ActionType.STICKER not in ticker._INTENT_EXECUTED_ACTIONS
    assert ActionType.IMAGE not in ticker._INTENT_EXECUTED_ACTIONS
