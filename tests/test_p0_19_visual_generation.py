from __future__ import annotations

import base64
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from character_memory.avatars import AvatarStore
from character_memory.media import MediaStorage
from character_memory.visual_generation import (
    AgnesImageProvider,
    ImageGenerationRequest,
    MsimgProvider,
    VisualPromptPlanner,
    VisualPurpose,
)


_PNG = b"\x89PNG\r\n\x1a\nvisual-test"


def test_agnes_provider_uses_reference_image_and_returns_base64():
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
            aspect_ratio="1:1",
            reference_images=["data:image/png;base64,AAAA"],
        )
    )

    assert seen["url"] == "https://apihub.agnes-ai.com/v1/images/generations"
    assert seen["authorization"] == "Bearer secret"
    assert seen["body"]["model"] == "agnes-image-2.5-flash"
    assert seen["body"]["return_base64"] is True
    assert seen["body"]["extra_body"]["image"] == ["data:image/png;base64,AAAA"]
    assert seen["body"]["extra_body"]["response_format"] == "b64_json"
    assert out.provider == "agnes"
    assert out.payload == _PNG
    assert out.mime_type == "image/png"
    client.close()


def test_agnes_provider_requires_key():
    provider = AgnesImageProvider("")
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


def test_visual_prompt_planner_is_structured_and_keeps_purpose():
    class FakeModel:
        def structured_for_session(self, prompt, schema, session_id):
            assert "purpose=SELFIE" in prompt
            assert "Has reference image: True" in prompt
            assert "不要决定是否应该发图" in prompt
            assert session_id == "visual-plan:mika:selfie"
            return {
                "purpose": "SELFIE",
                "visual_intent": "分享现在的自然状态",
                "positive_prompt": "same person, casual phone selfie, warm indoor light",
                "negative_prompt": "different person, changed hair color",
                "aspect_ratio": "3:4",
                "identity_constraints": ["same face", "same hair"],
            }

    plan = VisualPromptPlanner(FakeModel()).plan(
        "mika",
        purpose=VisualPurpose.SELFIE,
        persona="Mika, black hair, blue eyes",
        mental_state="relaxed",
        recent_dialogue=["用户: 在干嘛", "角色: 在看书"],
        visual_intent="想发一张现在的自拍",
        has_reference_image=True,
    )
    assert plan.purpose == VisualPurpose.SELFIE
    assert plan.aspect_ratio == "3:4"
    assert "same person" in plan.positive_prompt


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
