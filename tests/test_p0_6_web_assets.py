from pathlib import Path


def test_p0_6_unread_assets_are_wired_into_web_index():
    web = Path("src/character_memory/web")
    index = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "p0_6.js").read_text(encoding="utf-8")
    styles = (web / "p0_6.css").read_text(encoding="utf-8")

    assert "/static/p0_6.js" in index
    assert "/static/p0_6.css" in index
    assert "/v1/characters/summaries" in script
    assert "character-memory:last-read:" in script
    assert "unread-dot" in script
    assert ".unread-dot" in styles
