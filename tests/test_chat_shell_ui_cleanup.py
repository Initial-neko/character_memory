from pathlib import Path


def test_chat_header_has_one_search_entry_but_drawer_keeps_current_and_global_modes():
    web = Path("src/character_memory/web")
    html = (web / "index.html").read_text(encoding="utf-8")
    search = (web / "search.js").read_text(encoding="utf-8")

    assert html.count('id="conversationSearchButton"') == 1
    assert 'id="globalSearchButton"' not in html
    assert 'data-search-mode="current"' in search
    assert 'data-search-mode="global"' in search


def test_voice_call_card_owns_high_contrast_text_colors():
    css = Path("src/character_memory/web/voice.css").read_text(encoding="utf-8")

    assert ".voice-call-card" in css
    assert "background: #171a22" in css
    assert "color: #f6f7fb" in css
    assert ".voice-call-status" in css and "color: #eef1f7" in css
    assert ".voice-call-line-text" in css and "color: #f8f9fc" in css
    assert ".voice-call-transcript" in css and "color: #d9deea" in css
