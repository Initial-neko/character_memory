from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_sticker_picker_opens_caption_draft_before_sending():
    stickers = (WEB / "stickers.js").read_text(encoding="utf-8")

    assert "showStickerDraft(sticker);" in stickers
    assert 'id="stickerCaption"' in stickers
    assert "data-sticker-send" in stickers
    assert "data-sticker-draft-cancel" in stickers
    assert 'event.target.closest("#stickerCaption")' in stickers
    assert 'event.key !== "Enter" || event.shiftKey || event.isComposing' in stickers


def test_direct_sticker_send_forwards_caption_with_media():
    stickers = (WEB / "stickers.js").read_text(encoding="utf-8")

    assert 'async function sendDirect(sticker, caption = "")' in stickers
    assert 'message:text, sticker_id:sticker.id' in stickers
    assert 'content:text, sticker, sticker_id:sticker.id, action:text ? null : "STICKER"' in stickers


def test_group_sticker_send_forwards_caption_with_media():
    groups = (WEB / "groups.js").read_text(encoding="utf-8")

    assert 'async function sendSticker(sticker, caption = "")' in groups
    assert 'const text = String(caption || "").trim();' in groups
    assert '{message:text,sticker_id:sticker.id}' in groups
    assert 'content:text,action:text ? null : "STICKER"' in groups
