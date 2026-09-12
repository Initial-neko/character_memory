from pathlib import Path


def test_voice_call_uses_viewport_bounded_scrollable_layout():
    css = Path("src/character_memory/web/voice.css").read_text(encoding="utf-8")
    assert "height: min(740px, calc(100dvh - 48px))" in css
    assert ".voice-call-detail {" in css and "height: 100%; overflow: hidden" in css
    assert ".voice-call-log {" in css and "overflow-y: auto" in css
    assert "touch-action: pan-y" in css
    assert "scrollbar-gutter: stable" in css


def test_voice_transcript_and_mobile_card_remain_scrollable():
    css = Path("src/character_memory/web/voice.css").read_text(encoding="utf-8")
    assert ".voice-call-transcript {" in css and "max-height: 112px; overflow-y: auto" in css
    assert "height: calc(100dvh - 24px)" in css
    assert "grid-template-rows: auto minmax(0, 1fr)" in css
