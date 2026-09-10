from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_sticker_panel_stops_click_bubbling_before_pack_rerender():
    js = (WEB / "p0_7.js").read_text(encoding="utf-8")
    marker = 'stickerPanel.addEventListener("click", async event => {'
    start = js.index(marker)
    end = js.index('document.addEventListener("click"', start)
    handler = js[start:end]
    assert "event.stopPropagation();" in handler
    assert "renderStickerPanel(stickers, characterId);" in handler
