from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import zipfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.domain.models import Event, EventType
from character_memory.stickers import private_sticker_dir
from test_p0_10_web_sticker_manager import FakeStickerTagModel, _bundle, _tagged_zip


def _import(client, *, character="neko", data=None):
    return client.post(
        f"/v1/stickers/import?scope=character&character_id={character}&filename=cute.zip&auto_tag=false",
        content=data if data is not None else _tagged_zip(),
        headers={"Content-Type": "application/zip"},
    )


def test_private_import_isolated_from_global_other_characters_and_group_picker(tmp_path):
    bundle, runtimes, store = _bundle(tmp_path, FakeStickerTagModel())
    try:
        client = TestClient(create_api(bundle=bundle))
        response = _import(client)
        assert response.status_code == 200, response.text
        assert response.json()["scope"] == "character"
        assert response.json()["imported"] == 1

        neko = client.get("/v1/stickers?character_id=neko").json()["stickers"]
        momo = client.get("/v1/stickers?character_id=momo").json()["stickers"]
        global_items = client.get("/v1/stickers").json()["stickers"]
        custom = [item for item in neko if item["scope"] == "character"]
        assert len(custom) == 1
        sticker = custom[0]
        assert sticker["owner_character_id"] == "neko"
        assert sticker["id"].startswith("private_")
        assert sticker["id"] not in {item["id"] for item in momo}
        assert sticker["id"] not in {item["id"] for item in global_items}
        assert sticker["url"] == f'/v1/stickers/neko/{sticker["id"]}/asset'
        assert client.get(sticker["url"]).content == b"tagged-png"
        assert client.get(f'/v1/stickers/{sticker["id"]}/asset').status_code == 404
        assert client.get(f'/v1/stickers/momo/{sticker["id"]}/asset').status_code == 404
        assert client.post("/v1/chat", json={
            "character_id": "momo", "sticker_id": sticker["id"],
        }).status_code == 400

        assert runtimes["neko"].sticker_catalog.get(sticker["id"]) is not None
        assert runtimes["momo"].sticker_catalog.get(sticker["id"]) is None
        assert runtimes["neko"].sticker_catalog.get("round_cat_happy") is not None
        assert runtimes["momo"].sticker_catalog.get("round_cat_happy") is not None
        assert not (Path(bundle.settings.sticker_dir) / "manifest.yaml").exists()
        assert private_sticker_dir(bundle.settings.sticker_dir, "neko").joinpath("manifest.yaml").exists()
    finally:
        store.close()


def test_private_import_requires_valid_scope_and_known_character(tmp_path):
    bundle, _, store = _bundle(tmp_path, FakeStickerTagModel())
    try:
        client = TestClient(create_api(bundle=bundle))
        for query, status in (
            ("scope=character", 400),
            ("scope=unknown", 400),
            ("scope=character&character_id=ghost", 404),
        ):
            response = client.post(f"/v1/stickers/import?{query}", content=_tagged_zip(),
                                   headers={"Content-Type": "application/zip"})
            assert response.status_code == status, response.text
        assert not Path(bundle.settings.sticker_dir).exists()
    finally:
        store.close()


def test_identical_named_private_packs_do_not_collide_and_reimports_preserve_history(tmp_path):
    bundle, _, store = _bundle(tmp_path, FakeStickerTagModel())
    try:
        client = TestClient(create_api(bundle=bundle))
        assert _import(client).status_code == 200
        first = next(item for item in client.get("/v1/stickers?character_id=neko").json()["stickers"]
                     if item["scope"] == "character")
        assert _import(client).status_code == 200
        assert len([item for item in client.get("/v1/stickers?character_id=neko").json()["stickers"]
                    if item["scope"] == "character"]) == 1

        assert _import(client, character="momo").status_code == 200
        momo = next(item for item in client.get("/v1/stickers?character_id=momo").json()["stickers"]
                    if item["scope"] == "character")
        assert first["id"] != momo["id"]
        assert first["pack_id"] != momo["pack_id"]
        assert client.get(f'/v1/stickers/neko/{momo["id"]}/asset').status_code == 404

        changed_stream = BytesIO()
        with zipfile.ZipFile(BytesIO(_tagged_zip())) as original:
            with zipfile.ZipFile(changed_stream, "w") as replacement:
                for name in original.namelist():
                    payload = b"new-content" if name.endswith("cute_happy.png") else original.read(name)
                    replacement.writestr(name, payload)
        assert _import(client, data=changed_stream.getvalue()).status_code == 200
        updated = [item for item in client.get("/v1/stickers?character_id=neko").json()["stickers"]
                   if item["scope"] == "character"]
        assert len(updated) == 2
        assert client.get(first["url"]).content == b"tagged-png"
        second = next(item for item in updated if item["id"] != first["id"])
        assert client.get(second["url"]).content == b"new-content"

        now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        event = store.append_event(Event(
            character_id="neko", event_type=EventType.CHARACTER_MESSAGE,
            event_time=now, content="[表情包]",
            metadata={"action": "STICKER", "sticker_id": first["id"],
                      "sticker_label": first["label"], "conversation_id": "example"},
        ))
        history = client.get("/v1/chat/history?character_id=neko").json()["messages"]
        message = next(item for item in history if item["id"] == event.id)
        assert message["sticker"]["url"] == first["url"]
        assert client.get(message["sticker"]["url"]).content == b"tagged-png"
    finally:
        store.close()
