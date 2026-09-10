from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

from character_memory.stickers import import_sticker_bundle, load_global_sticker_catalog, load_sticker_catalog


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def _tagged_zip() -> bytes:
    rows = [
        {
            "set_id": "set_01",
            "set_name": "base_emotions",
            "display_name": "基础情绪包",
            "id": "set_01_01",
            "filename": "set_01_01_happy_smile.png",
            "category": "happy",
            "tag_zh": "开心微笑",
            "tag_en": "happy_smile",
            "aliases": ["开心", "高兴", "微笑"],
            "description": "轻松开心。",
        },
        {
            "set_id": "set_02",
            "set_name": "daily_chat",
            "display_name": "日常聊天包",
            "id": "set_02_01",
            "filename": "set_02_01_hello_wave.png",
            "category": "daily",
            "tag_zh": "挥手你好",
            "tag_en": "hello_wave",
            "aliases": ["你好", "挥手"],
            "description": "日常打招呼。",
        },
    ]
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bundle/all_tags.json", json.dumps(rows, ensure_ascii=False))
        archive.writestr("bundle/split_stickers/set_01_base_emotions/set_01_01_happy_smile.png", b"png-one")
        archive.writestr("bundle/split_stickers/set_02_daily_chat/set_02_01_hello_wave.png", b"png-two")
    return output.getvalue()


def test_tagged_sticker_zip_imports_and_keeps_default_pack(tmp_path):
    persona = tmp_path / "personas" / "neko" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("name: Neko\n", encoding="utf-8")

    result = import_sticker_bundle(persona, _tagged_zip())
    assert result["imported"] == 2
    assert {item["id"] for item in result["packs"]} == {"set_01", "set_02"}

    catalog = load_sticker_catalog(persona)
    custom = catalog.get("set_01_01")
    assert custom is not None
    assert custom.label == "开心微笑"
    assert custom.pack_name == "基础情绪包"
    assert "开心" in custom.tags
    assert catalog.asset_path("set_01_01").read_bytes() == b"png-one"
    assert catalog.get("round_cat_happy") is not None


def test_global_target_dir_makes_import_visible_independent_of_character(tmp_path):
    persona_a = tmp_path / "personas" / "a" / "persona.yaml"
    persona_b = tmp_path / "personas" / "b" / "persona.yaml"
    persona_a.parent.mkdir(parents=True)
    persona_b.parent.mkdir(parents=True)
    persona_a.write_text("name: A\n", encoding="utf-8")
    persona_b.write_text("name: B\n", encoding="utf-8")
    global_dir = tmp_path / "data" / "stickers"

    import_sticker_bundle(persona_a, _tagged_zip(), target_dir=global_dir)
    catalog = load_global_sticker_catalog(global_dir, persona_paths=[persona_a, persona_b])
    assert catalog.get("set_01_01") is not None
    assert catalog.get("set_02_01") is not None
    assert catalog.asset_path("set_01_01").read_bytes() == b"png-one"


def test_sticker_import_rejects_unsafe_zip_paths(tmp_path):
    persona = tmp_path / "personas" / "neko" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("name: Neko\n", encoding="utf-8")
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape.png", b"bad")
        archive.writestr("all_tags.json", "[]")
    with pytest.raises(ValueError, match="unsafe zip path"):
        import_sticker_bundle(persona, output.getvalue())


def test_web_supports_clipboard_images_and_core_sticker_rendering():
    images = (WEB / "images.js").read_text(encoding="utf-8")
    core = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("paste"' in images
    assert 'item.kind === "file"' in images
    assert 'startsWith("image/")' in images
    assert 'source:"CLIPBOARD"' in images
    assert "/v1/stickers/${encodeURIComponent(message.sticker_id)}/asset" in core
    assert ".sticker-bubble img" in core


def test_sticker_picker_has_global_pack_tabs_and_small_previews():
    js = (WEB / "stickers.js").read_text(encoding="utf-8")
    css = (WEB / "p0_7.css").read_text(encoding="utf-8")
    assert "data-sticker-pack" in js
    assert "pack_name" in js
    assert 'CM.registerFeature("stickers"' in js
    assert 'CM.api("/v1/stickers")' in js
    assert "character_id" not in js[js.index('CM.api("/v1/stickers")') - 120:js.index('CM.api("/v1/stickers")') + 120]
    assert "grid-template-columns: repeat(6, 52px)" in css
    assert "max-width: 112px" in css
