from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.stickers import load_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


class FakeStickerTagModel:
    def __init__(self):
        self.calls = []

    def structured_with_images_for_session(self, prompt, image_data_urls, schema, session_id):
        self.calls.append({"prompt": prompt, "images": image_data_urls, "session_id": session_id})
        return schema(label="震惊", tags=["震惊", "意外", "懵"], description="适合表达突然被惊到或一时没反应过来。")


def _persona(tmp_path, character_id):
    persona = tmp_path / "personas" / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text(f"id: {character_id}\nname: {character_id.title()}\nidentity: test\n", encoding="utf-8")
    return persona


def _bundle(tmp_path, model):
    store = SQLiteStore(tmp_path / "x.db")
    personas = {character_id: _persona(tmp_path, character_id) for character_id in ("neko", "momo")}
    settings = SimpleNamespace(
        chat_model="deepseek-flash",
        vision_model="deepseek-v4-flash-vision-exp",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=str(tmp_path / "x.db"),
        media_dir=str(tmp_path / "media"),
        media_max_bytes=8 * 1024 * 1024,
        sticker_dir=str(tmp_path / "global-stickers"),
        base_url="fake",
        persona_path=str(personas["neko"]),
        api_key="",
        recall_limit=8,
    )
    runtimes = {
        character_id: SimpleNamespace(sticker_catalog=load_sticker_catalog(persona))
        for character_id, persona in personas.items()
    }
    profiles = [
        {"id": character_id, "name": character_id.title(), "identity": "", "tagline": "", "persona_path": str(persona)}
        for character_id, persona in personas.items()
    ]
    bundle = SimpleNamespace(
        settings=settings,
        store=store,
        characters=profiles,
        runtimes=runtimes,
        model=model,
    )
    return bundle, runtimes, store


def _tagged_zip() -> bytes:
    rows = [{
        "set_id": "cute",
        "display_name": "可爱包",
        "id": "cute_happy",
        "filename": "cute_happy.png",
        "tag_zh": "开心",
        "tag_en": "happy",
        "aliases": ["高兴", "好耶"],
        "description": "开心时使用。",
    }]
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("all_tags.json", json.dumps(rows, ensure_ascii=False))
        archive.writestr("split/cute_happy.png", b"tagged-png")
    return output.getvalue()


def _untagged_zip() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("wow.png", b"untagged-png")
    return output.getvalue()


def test_web_import_uses_existing_tags_without_vision_and_hot_refreshes_every_runtime(tmp_path):
    model = FakeStickerTagModel()
    bundle, runtimes, store = _bundle(tmp_path, model)
    client = TestClient(create_api(bundle=bundle))

    response = client.post(
        "/v1/stickers/import?character_id=neko&filename=my-pack.zip&auto_tag=true",
        content=_tagged_zip(),
        headers={"Content-Type": "application/zip"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope"] == "global"
    assert payload["imported"] == 1
    assert payload["ai_tagged"] == 0
    assert model.calls == []
    assert (Path(bundle.settings.sticker_dir) / "manifest.yaml").is_file()
    for runtime in runtimes.values():
        imported = runtime.sticker_catalog.get("cute_happy")
        assert imported is not None
        assert imported.label == "开心"
        assert runtime.sticker_catalog.asset_path("cute_happy").read_bytes() == b"tagged-png"

    neko = client.get("/v1/stickers?character_id=neko").json()
    momo = client.get("/v1/stickers?character_id=momo").json()
    assert neko["scope"] == momo["scope"] == "global"
    assert [item["id"] for item in neko["stickers"]] == [item["id"] for item in momo["stickers"]]
    selected = next(item for item in momo["stickers"] if item["id"] == "cute_happy")
    assert selected["url"] == "/v1/stickers/cute_happy/asset"
    assert client.get(selected["url"]).content == b"tagged-png"
    store.close()


def test_web_import_can_ai_tag_image_only_zip_and_make_it_global(tmp_path):
    model = FakeStickerTagModel()
    bundle, runtimes, store = _bundle(tmp_path, model)
    client = TestClient(create_api(bundle=bundle))

    response = client.post(
        "/v1/stickers/import?filename=raw-reactions.zip&auto_tag=true",
        content=_untagged_zip(),
        headers={"Content-Type": "application/zip"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["imported"] == 1
    assert payload["ai_tagged"] == 1
    assert len(model.calls) == 1
    assert model.calls[0]["images"][0].startswith("data:image/png;base64,")
    for runtime in runtimes.values():
        imported = runtime.sticker_catalog.get("wow")
        assert imported is not None
        assert imported.label == "震惊"
        assert "意外" in imported.tags
    store.close()


def test_web_import_without_metadata_requires_auto_tag(tmp_path):
    model = FakeStickerTagModel()
    bundle, _, store = _bundle(tmp_path, model)
    client = TestClient(create_api(bundle=bundle))

    response = client.post(
        "/v1/stickers/import?filename=raw.zip&auto_tag=false",
        content=_untagged_zip(),
        headers={"Content-Type": "application/zip"},
    )
    assert response.status_code == 400
    assert "enable AI auto-tag" in response.json()["detail"]
    store.close()


def test_sticker_web_manager_uses_global_library_and_no_character_cache_key():
    js = (WEB / "stickers.js").read_text(encoding="utf-8")
    context = (ROOT / "src" / "character_memory" / "runtime" / "context.py").read_text(encoding="utf-8")

    assert "/v1/stickers/import" in js
    assert '"Content-Type":"application/zip"' in js
    assert "导入全局表情包" in js
    assert "所有人物和群聊都能使用" in js
    assert 'CM.api("/v1/stickers")' in js
    assert "stickerCache = new Map" not in js
    assert "cache.get(CM.state.characterId)" not in js
    assert "cache.set(CM.state.characterId" not in js
    assert "不需要等用户先发表情包" in context
    assert "MESSAGE + STICKER" in context
