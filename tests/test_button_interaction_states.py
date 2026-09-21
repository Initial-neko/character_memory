from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_shared_button_states_do_not_outrank_feature_components():
    ui = (WEB / "ui.css").read_text(encoding="utf-8")

    # The shared baseline is a fallback. A selector such as
    # button:hover:not(:disabled) has greater specificity than
    # .voice-visual-button:hover and used to repaint dark controls white.
    assert ":where(button:hover:not(:disabled), .ui-btn:hover:not(:disabled))" in ui
    assert re.search(r"(?m)^\s*button:hover:not\(:disabled\)\s*,", ui) is None
    assert ":where(\n  button:focus-visible," in ui
    assert "-webkit-appearance: none" in ui
    assert "appearance: none" in ui


def test_dark_voice_controls_keep_component_owned_focus_visuals():
    visual = (WEB / "visual_capture.css").read_text(encoding="utf-8")
    voice = (WEB / "voice.css").read_text(encoding="utf-8")

    assert ".voice-visual-button:focus-visible" in visual
    assert "box-shadow: 0 0 0 3px rgba(103,151,255,.18)" in visual
    assert ".voice-call-card :where(button:focus-visible)" in voice
    assert "box-shadow: 0 0 0 3px rgba(132,166,255,.24)" in voice
