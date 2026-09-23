from __future__ import annotations

import base64
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from character_memory.avatars import AvatarStore
from character_memory.media import MediaStorage
from character_memory.storage.sqlite import SQLiteStore
from character_memory.visual_generation import (
    AgnesImageProvider,
    ImageGenerationRequest,
    ImageGenerationResult,
    MsimgProvider,
    VisualPromptPlanner,
    VisualPurpose,
    visual_aspect_ratio,
)
from character_memory.visual_web import attach_visual_routes


_PNG = b"\x89PNG\r\n\x1a\nvisual-test"


def test_agnes_provider_maps_ratio_to_explicit_size_and_returns_base64():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode("ascii")}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AgnesImageProvider("secret", client=client)
    out = provider.generate(
        ImageGenerationRequest(
            prompt="same character, natural portrait",
            aspect_ratio="3:4",
            reference_images=["data:image/png;base64,AAAA"],
        )
    )

    assert seen["url"] == "https://apihub.agnes-ai.com/v1/images/generations"
    assert seen["authorization"] == "Bearer secret"
    assert seen["body"]["model"] == "agnes-image-2.1-flash"
    assert seen["body"]["size"] == "768x1024"
    assert "ratio" not in seen["body"]
    assert seen["body"]["return_base64"] is True
    assert seen["body"]["extra_body"]["image"] == ["data:image/png;base64,AAAA"]
    assert seen["body"]["extra_body"]["response_format"] == "b64_json"
    assert out.provider == "agnes"
    assert out.payload == _PNG
    assert out.mime_type == "image/png"
    client.close()


def test_agnes_provider_requires_key():
    provider = AgnesImageProvider("")
    assert provider.configured() is False
    assert provider.available() is False
    with pytest.raises(RuntimeError, match="AGNES_API_KEY"):
        provider.generate(ImageGenerationRequest(prompt="test"))
    provider.close()


def test_msimg_provider_is_lazy_and_maps_pil_like_image(monkeypatch):
    seen = {}

    class FakeImage:
        size = (640, 640)

        def save(self, buffer, format):
            assert format == "PNG"
            buffer.write(_PNG)

    def generate_image(**kwargs):
        seen.update(kwargs)
        return {"image": FakeImage(), "model": "mock-qwen"}

    monkeypatch.setitem(sys.modules, "msimg", SimpleNamespace(generate_image=generate_image))
    provider = MsimgProvider("modelscope-secret", models="qwen,flux-majic")
    assert provider.configured() is True
    assert provider.available() is True
    out = provider.generate(ImageGenerationRequest(prompt="portrait", aspect_ratio="3:4"))

    assert seen["api_configs"] == "modelscope-secret"
    assert seen["models"] == ["qwen", "flux-majic"]
    assert seen["size"] == "3:4"
    assert seen["enable_failover"] is True
    assert out.provider == "msimg"
    assert out.model == "mock-qwen"
    assert out.payload == _PNG
    assert (out.width, out.height) == (640, 640)
    provider.close()


def test_msimg_provider_rejects_reference_images_before_import():
    provider = MsimgProvider("secret")
    with pytest.raises(RuntimeError, match="does not expose a reference-image contract"):
        provider.generate(ImageGenerationRequest(prompt="selfie", reference_images=["data:image/png;base64,AAAA"]))
    provider.close()


def test_visual_prompt_planner_requests_plain_text_and_adds_reference_policy():
    seen = {}

    class FakeModel:
        def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
            seen["messages"] = messages
            seen["conversation_id"] = conversation_id
            seen["json_object"] = json_object
            return "same person, casual phone selfie, warm indoor light"

    prompt = VisualPromptPlanner(FakeModel()).compile_prompt(
        "mika",
        purpose=VisualPurpose.SELFIE,
        persona="Mika, black hair, blue eyes",
        mental_state="relaxed",
        recent_dialogue=["用户: 在干嘛", "角色: 在看书"],
        visual_intent="想发一张现在的自拍",
        has_reference_image=True,
    )

    assert seen["conversation_id"] == "visual-plan:mika:selfie"
    assert seen["json_object"] is False
    assert "不返回 JSON" in seen["messages"][0]["content"]
    assert "只输出最终绘图提示词纯文本" in seen["messages"][1]["content"]
    assert "same person" in prompt
    assert "reference image as the identity anchor" in prompt


def test_visual_prompt_planner_strips_code_fence_without_schema_validation():
    class FakeModel:
        def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
            return "```text\na calm rainy street at night\n```"

    prompt = VisualPromptPlanner(FakeModel()).compile_prompt(
        "mika",
        purpose=VisualPurpose.SCENE,
        persona="Mika",
        visual_intent="画一个安静雨夜",
    )
    assert prompt == "a calm rainy street at night"


def test_visual_aspect_ratio_is_application_owned():
    assert visual_aspect_ratio(VisualPurpose.AVATAR) == "1:1"
    assert visual_aspect_ratio(VisualPurpose.SELFIE) == "3:4"
    assert visual_aspect_ratio(VisualPurpose.SCENE) == "4:3"
    assert visual_aspect_ratio(VisualPurpose.STICKER) == "1:1"


def test_generated_media_can_be_copied_to_avatar_with_provenance(tmp_path):
    media = MediaStorage(tmp_path / "media")
    asset = media.save_bytes(
        character_id="mika",
        original_name="selfie.png",
        payload=_PNG,
        created_at=__import__("datetime").datetime.now().astimezone(),
        source="GENERATED_AVATAR_CANDIDATE",
    )
    assert asset.source == "GENERATED_AVATAR_CANDIDATE"
    path = media.asset_path(asset)
    assert path is not None

    avatars = AvatarStore(tmp_path / "avatars")
    metadata = avatars.save_from_path(
        "mika",
        path,
        source="GENERATED",
        source_media_id=asset.id,
        title="better selfie",
    )
    assert metadata.source == "GENERATED"
    assert metadata.source_media_id == asset.id
    assert metadata.title == "better selfie"
    assert avatars.asset_path("mika").read_bytes() == _PNG
    avatars.close()


class _AvatarBatchProvider:
    supports_reference_images = False

    def __init__(self, *, fail_at: int | None = None):
        self.fail_at = fail_at
        self.calls = 0

    def configured(self):
        return True

    def available(self):
        return True

    def generate(self, request):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("provider rate limited")
        return ImageGenerationResult(
            provider="fake",
            model="fake-image",
            payload=_PNG,
            mime_type="image/png",
        )


class _AvatarPromptModel:
    def _request(self, messages, *, conversation_id=None, json_object=False, model=None):
        assert conversation_id == "visual-plan:mika:avatar"
        return "same character, stable portrait identity, clean avatar composition"


def _avatar_batch_app(tmp_path, provider):
    store = SQLiteStore(tmp_path / "avatar-batch.db")
    media = MediaStorage(tmp_path / "media")
    avatars = AvatarStore(tmp_path / "avatars")
    runtime = SimpleNamespace(persona="Mika, black hair, blue eyes")
    bundle = SimpleNamespace(
        model=_AvatarPromptModel(),
        runtimes={"mika": runtime},
    )
    access = SimpleNamespace(
        settings=SimpleNamespace(image_generation_provider="fake"),
        services=SimpleNamespace(
            avatar_store=avatars,
            image_generation_providers={"fake": provider},
        ),
        read_store=store,
        media_storage=media,
        character_profiles=lambda: [
            {
                "id": "mika",
                "name": "Mika",
                "identity": "tester",
                "persona_path": "unused.yaml",
            }
        ],
        require_bundle=lambda: bundle,
        store=lambda: store,
    )
    app = FastAPI()
    app.state.character_memory = access
    attach_visual_routes(app)
    return app, store, media, avatars


def test_avatar_candidate_batch_reuses_image_prompt_polish_contract():
    from character_memory.visual_web import AvatarGenerateRequest

    request = AvatarGenerateRequest(
        provider="agnes",
        hint="更温暖一点",
        style="anime_clean",
        count=4,
    )
    assert request.style == "ANIME_CLEAN"
    assert request.count == 4

    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "character_memory"
        / "visual_web.py"
    ).read_text(encoding="utf-8")
    assert "rewrite_req = ImageRewriteRequest(" in source
    assert "compile_instruction(" in source
    assert '"candidates": candidates' in source
    assert "GENERATED_AVATAR_CANDIDATE" in source
    assert "_AVATAR_STYLE_GUIDANCE" in source


def test_avatar_candidate_batch_api_persists_exact_requested_count(tmp_path):
    provider = _AvatarBatchProvider()
    app, store, media, avatars = _avatar_batch_app(tmp_path, provider)

    with TestClient(app) as client:
        response = client.post(
            "/v1/characters/mika/avatar/generate",
            json={
                "provider": "fake",
                "style": "ANIME_CLEAN",
                "count": 4,
                "hint": "温暖一点",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["candidates"]) == 4
    assert provider.calls == 4
    media_ids = [item["media_id"] for item in body["candidates"]]
    assert len(set(media_ids)) == 4
    assert all(store.get_media_asset(media_id) is not None for media_id in media_ids)
    assert all(media.asset_path(store.get_media_asset(media_id)) is not None for media_id in media_ids)
    with store._lock:
        total = store.conn.execute("SELECT COUNT(*) AS total FROM media_assets").fetchone()["total"]
    assert total == 4
    avatars.close()
    store.close()


def test_avatar_candidate_batch_failure_rolls_back_db_and_files(tmp_path):
    provider = _AvatarBatchProvider(fail_at=3)
    app, store, media, avatars = _avatar_batch_app(tmp_path, provider)

    with TestClient(app) as client:
        response = client.post(
            "/v1/characters/mika/avatar/generate",
            json={"provider": "fake", "count": 4},
        )

    assert response.status_code == 502
    assert "provider rate limited" in response.json()["detail"]
    assert provider.calls == 3
    with store._lock:
        total = store.conn.execute("SELECT COUNT(*) AS total FROM media_assets").fetchone()["total"]
    assert total == 0
    assert not media.root.exists() or list(media.root.iterdir()) == []
    avatars.close()
    store.close()


def test_avatar_candidate_batch_rejects_unknown_style():
    from pydantic import ValidationError
    from character_memory.visual_web import AvatarGenerateRequest

    with pytest.raises(ValidationError, match="avatar style must be one of"):
        AvatarGenerateRequest(style="oil-painting-by-random-name")
