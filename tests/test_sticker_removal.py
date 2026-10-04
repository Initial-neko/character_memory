from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.async_web import attach_async_routes
from character_memory.stickers import load_global_sticker_catalog, remove_global_stickers
import character_memory.stickers as sticker_module
from test_p0_10_web_sticker_manager import _bundle, _tagged_zip, FakeStickerTagModel


def test_removal_persists_for_builtin_and_legacy_and_keeps_assets(tmp_path):
    root = tmp_path / "global"
    persona = tmp_path / "persona.yaml"
    legacy = tmp_path / "stickers"
    legacy.mkdir()
    (legacy / "old.svg").write_text("<svg/>")
    (legacy / "manifest.yaml").write_text(yaml.safe_dump({"stickers": [
        {"id": "legacy", "file": "old.svg", "label": "旧表情", "pack_id": "legacy"}
    ]}), encoding="utf-8")
    for sticker_id in ("round_cat_happy", "legacy"):
        assert remove_global_stickers(root, sticker_id=sticker_id, persona_paths=[persona])["removed"] == 1
        assert remove_global_stickers(root, sticker_id=sticker_id, persona_paths=[persona])["removed"] == 0
    catalog = load_global_sticker_catalog(root, persona_paths=[persona])
    for sticker_id in ("round_cat_happy", "legacy"):
        assert catalog.get(sticker_id) is None
        assert catalog.historical_get(sticker_id) is not None
        assert catalog.asset_path(sticker_id).is_file()
        assert sticker_id not in catalog.prompt_text()
        assert sticker_id not in {item["id"] for item in catalog.public_items()}
    assert set(json.loads((root / "removed.json").read_text())) == {"round_cat_happy", "legacy"}


def test_failed_atomic_publication_keeps_previous_selection(tmp_path, monkeypatch):
    root = tmp_path / "global"
    remove_global_stickers(root, sticker_id="round_cat_happy")
    previous = (root / "removed.json").read_bytes()
    def fail_replace(*args):
        raise OSError("simulated publication failure")
    monkeypatch.setattr(sticker_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        remove_global_stickers(root, sticker_id="round_duck_shock")
    assert (root / "removed.json").read_bytes() == previous
    assert load_global_sticker_catalog(root).get("round_duck_shock") is not None
    assert not list(root.glob("*.tmp"))


def test_concurrent_removals_do_not_lose_ids(tmp_path):
    ids = [item.id for item in load_global_sticker_catalog(tmp_path).stickers]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda sticker_id: remove_global_stickers(tmp_path, sticker_id=sticker_id), ids))
    assert sum(result["removed"] for result in results) == len(ids)
    assert not load_global_sticker_catalog(tmp_path).public_items()
    assert set(json.loads((tmp_path / "removed.json").read_text())) == set(ids)


def test_delete_route_refreshes_all_runtimes_and_reimport_does_not_resurrect(tmp_path):
    bundle, runtimes, store = _bundle(tmp_path, FakeStickerTagModel())
    app = create_api(bundle=bundle)
    attach_async_routes(app)
    with TestClient(app) as client:
        imported = client.post("/v1/stickers/import?auto_tag=false", content=_tagged_zip(), headers={"Content-Type": "application/zip"})
        assert imported.status_code == 200
        asset_url = next(item["url"] for item in imported.json()["stickers"] if item["id"] == "cute_happy")
        original = client.get(asset_url).content
        stale_catalog = runtimes["neko"].sticker_catalog
        response = client.delete("/v1/stickers?pack_id=cute")
        assert response.status_code == 200
        assert response.json()["removed"] == 1
        assert "cute_happy" not in {item["id"] for item in response.json()["stickers"]}
        for runtime in runtimes.values():
            assert runtime.sticker_catalog.get("cute_happy") is None
            assert runtime.sticker_catalog.historical_get("cute_happy").label == "开心"
        assert client.get(asset_url).content == original
        assert client.get("/v1/stickers/neko/cute_happy/asset").content == original
        app.state.character_memory.refresh_runtime_sticker_catalog(stale_catalog)
        assert all(runtime.sticker_catalog.get("cute_happy") is None for runtime in runtimes.values())
        assert client.delete("/v1/stickers?pack_id=cute").json()["removed"] == 0
        assert client.post("/v1/stickers/import?auto_tag=false", content=_tagged_zip(), headers={"Content-Type": "application/zip"}).status_code == 200
        assert "cute_happy" not in {item["id"] for item in client.get("/v1/stickers").json()["stickers"]}
        assert client.post("/v1/chat/messages", json={"character_id": "neko", "sticker_id": "cute_happy", "message": ""}).status_code == 400
    store.close()


@pytest.mark.parametrize("query,status", [
    ("", 400), ("sticker_id=x&pack_id=x", 400), ("sticker_id=missing", 404),
    ("pack_id=missing", 404), ("sticker_id=../manifest.yaml", 400), ("pack_id=", 400),
])
def test_invalid_delete_never_mutates_library(tmp_path, query, status):
    bundle, _, store = _bundle(tmp_path, FakeStickerTagModel())
    with TestClient(create_api(bundle=bundle)) as client:
        assert client.delete(f"/v1/stickers?{query}").status_code == status
        assert not Path(bundle.settings.sticker_dir).exists()
    store.close()
