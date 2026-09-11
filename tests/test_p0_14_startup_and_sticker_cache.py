from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.startup_web import attach_startup_routes
from character_memory.stickers import clear_global_sticker_catalog_cache, load_global_sticker_catalog


def _write_manifest(root: Path, rows: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(
        yaml.safe_dump({"stickers": rows}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    for row in rows:
        (root / row["file"]).write_bytes(b"asset")


def test_global_sticker_catalog_reuses_cache_until_manifest_changes(tmp_path):
    clear_global_sticker_catalog_cache()
    root = tmp_path / "stickers"
    first_rows = [
        {
            "id": "cache_one",
            "file": "one.png",
            "label": "一",
            "tags": ["测试"],
            "pack_id": "cache",
            "pack_name": "缓存测试",
        }
    ]
    _write_manifest(root, first_rows)

    first = load_global_sticker_catalog(root)
    second = load_global_sticker_catalog(root)
    assert second is first
    assert first.get("cache_one") is not None

    second_rows = [
        *first_rows,
        {
            "id": "cache_two",
            "file": "two.png",
            "label": "二二",
            "tags": ["变化"],
            "pack_id": "cache",
            "pack_name": "缓存测试",
        },
    ]
    _write_manifest(root, second_rows)

    refreshed = load_global_sticker_catalog(root)
    assert refreshed is not first
    assert refreshed.get("cache_two") is not None


def test_runtime_warmup_initializes_bundle_and_refreshes_stickers():
    app = FastAPI()
    bundle = SimpleNamespace(
        characters=[{"id": "rin"}, {"id": "momo"}],
        init_timings={"total_ms": 12.5},
    )
    catalog = SimpleNamespace(stickers=[object(), object(), object()])
    calls = {"bundle": 0, "catalog": 0, "refresh": 0}

    def get_bundle():
        calls["bundle"] += 1
        return bundle

    def global_sticker_catalog():
        calls["catalog"] += 1
        return catalog

    def refresh_runtime_sticker_catalog(value):
        assert value is catalog
        calls["refresh"] += 1

    app.state.character_memory = SimpleNamespace(
        get_bundle=get_bundle,
        global_sticker_catalog=global_sticker_catalog,
        refresh_runtime_sticker_catalog=refresh_runtime_sticker_catalog,
    )
    attach_startup_routes(app)

    response = TestClient(app).post("/v1/runtime/warmup")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["runtime_loaded"] is True
    assert body["characters"] == 2
    assert body["stickers"] == 3
    assert body["init_timings"]["total_ms"] == 12.5
    assert calls == {"bundle": 1, "catalog": 1, "refresh": 1}


def test_web_index_loads_startup_gate_before_chat_controller():
    web = Path(__file__).parents[1] / "src" / "character_memory" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    startup = (web / "startup.js").read_text(encoding="utf-8")

    assert 'id="startupGate"' in index
    assert index.index('/static/startup.js') < index.index('/static/app.js')
    assert 'fetch("/v1/runtime/warmup"' in startup
    assert 'id="startupRetry"' in index
    assert 'id="startupContinue"' in index
