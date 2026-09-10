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
