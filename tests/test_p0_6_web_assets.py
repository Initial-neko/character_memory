from pathlib import Path


def test_unread_module_is_wired_without_overriding_core_chat_functions():
    web = Path("src/character_memory/web")
    index = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "unread.js").read_text(encoding="utf-8")
    styles = (web / "p0_6.css").read_text(encoding="utf-8")

    assert "/static/unread.js" in index
    assert "/static/p0_6.js" not in index
    assert "/v1/characters/summaries" in script
    assert "character-memory:last-read:" in script
    assert 'CM.registerFeature("unread"' in script
    assert "renderCharacterList =" not in script
    assert ".unread-dot" in styles
