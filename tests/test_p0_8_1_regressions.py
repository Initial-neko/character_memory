from pathlib import Path

from character_memory.domain.models import ActionType, PersonReaction


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_provider_text_alias_is_normalized_to_canonical_message():
    result = PersonReaction.model_validate(
        {
            "actions": [
                {"type": "MESSAGE", "text": "嗯。"},
                {"type": "MESSAGE", "text": "所以你最后到底擦了没。"},
            ]
        }
    )

    assert [action.type for action in result.actions] == [ActionType.MESSAGE, ActionType.MESSAGE]
    assert [action.message for action in result.actions] == ["嗯。", "所以你最后到底擦了没。"]
    dumped = result.model_dump(mode="json")
    assert dumped["actions"][0]["message"] == "嗯。"
    assert "text" not in dumped["actions"][0]


def test_provider_content_alias_and_null_summaries_are_normalized():
    # Regression for a real DeepSeek/OpenCode response shape observed in local chat:
    # mental_state_update=null and MESSAGE content stored under `content`.
    result = PersonReaction.model_validate(
        {
            "perception": None,
            "reaction": None,
            "mental_state_update": None,
            "actions": [
                {"type": "MESSAGE", "content": "。你在哇什么。", "reason": None},
            ],
            "memory_candidates": None,
            "intent_candidates": None,
        }
    )

    assert result.perception == ""
    assert result.reaction == ""
    assert result.mental_state_update == ""
    assert result.actions[0].message == "。你在哇什么。"
    assert result.actions[0].reason == ""
    assert result.memory_candidates == []
    assert result.intent_candidates == []
    dumped = result.model_dump(mode="json")
    assert dumped["actions"][0]["message"] == "。你在哇什么。"
    assert "content" not in dumped["actions"][0]


def test_null_actions_is_treated_as_explicit_silence_but_missing_contract_is_not():
    result = PersonReaction.model_validate({"actions": None})
    assert result.actions == []
    assert result.action.type == ActionType.NO_REPLY

    try:
        PersonReaction.model_validate({"mental_state_update": None})
    except ValueError:
        pass
    else:
        raise AssertionError("missing actions/action must still be rejected")


def test_empty_message_remains_invalid_after_alias_normalization():
    try:
        PersonReaction.model_validate({"actions": [{"type": "MESSAGE", "content": "   "}]})
    except ValueError:
        pass
    else:
        raise AssertionError("empty MESSAGE must remain invalid")


def test_sticker_css_does_not_break_original_fixed_composer_layout():
    css = (WEB / "p0_7.css").read_text(encoding="utf-8")
    assert ".composer-wrap { position: relative; }" not in css
    assert "grid-template-columns: repeat(6, 52px)" in css
    assert "max-width: 112px" in css


def test_media_buttons_override_generic_send_button_size():
    css = (WEB / "p0_8.css").read_text(encoding="utf-8")
    assert ".composer .sticker-trigger" in css
    assert ".composer .image-trigger" in css
    assert "min-width: 36px" in css
