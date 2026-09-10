import base64
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.storage.sqlite import SQLiteStore


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def test_character_image_catalog_can_be_served(tmp_path):
    persona_dir = tmp_path / "personas" / "momo"
    images_dir = persona_dir / "images"
    images_dir.mkdir(parents=True)
    persona = persona_dir / "persona.yaml"
    persona.write_text("id: momo\nname: Momo\n", encoding="utf-8")
    (images_dir / "tea.png").write_bytes(PNG_1X1)
    (images_dir / "manifest.yaml").write_text(
        "images:\n"
        "  - id: tea_photo\n"
        "    file: tea.png\n"
        "    label: 下午茶照片\n"
        "    tags: [分享, 下午茶]\n",
        encoding="utf-8",
    )

    store = SQLiteStore(tmp_path / "x.db")
    settings = SimpleNamespace(
        chat_model="deepseek-flash",
        vision_model="deepseek-v4-flash-vision-exp",
        embedding_provider="deterministic",
        embedding_model="deterministic",
        db_path=store.path,
        media_dir=str(tmp_path / "media"),
        media_max_bytes=8 * 1024 * 1024,
        base_url="fake",
        persona_path=str(persona),
        api_key="",
    )
    bundle = SimpleNamespace(
        settings=settings,
        store=store,
        characters=[{"id": "momo", "name": "Momo", "identity": "", "tagline": "", "persona_path": str(persona)}],
    )
    client = TestClient(create_api(bundle=bundle))

    response = client.get("/v1/images?character_id=momo")
    assert response.status_code == 200
    item = response.json()["images"][0]
    assert item["id"] == "tea_photo"
    assert item["label"] == "下午茶照片"
    asset = client.get(item["url"])
    assert asset.status_code == 200
    assert asset.content == PNG_1X1
    store.close()
