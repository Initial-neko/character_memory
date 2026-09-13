from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.config import Settings
from character_memory.dev_server import create_dev_app


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""
        self.content = b""
        self.headers = {}

    @property
    def is_error(self):
        return self.status_code >= 400

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeVisualHttpClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/v1/characters"):
            return FakeResponse({"characters": [{"id": "mika", "name": "Mika"}]})
        if url.endswith("/v1/visual/providers"):
            return FakeResponse(
                {
                    "default": "agnes",
                    "providers": [
                        {
                            "id": "agnes",
                            "configured": True,
                            "available": True,
                            "model": "agnes-image-2.1-flash",
                            "supports_reference_images": True,
                            "reason": "",
                        }
                    ],
                }
            )
        if url.endswith("/v1/characters/mika/images/rewrite"):
            request = kwargs.get("json") or {}
            return FakeResponse(
                {
                    "ok": True,
                    "character_id": "mika",
                    "purpose": request.get("purpose") or "SELFIE",
                    "provider": request.get("provider") or "agnes",
                    "instruction": request.get("instruction") or "",
                    "prompt": "same person, natural casual selfie",
                    "aspect_ratio": "3:4",
                    "used_avatar_reference": True,
                    "duration_ms": 220.0,
                }
            )
        if url.endswith("/v1/characters/mika/images/generate"):
            request = kwargs.get("json") or {}
            return FakeResponse(
                {
                    "ok": True,
                    "character_id": "mika",
                    "purpose": request.get("purpose") or "SELFIE",
                    "provider": "agnes",
                    "model": "agnes-image-2.1-flash",
                    "instruction": request.get("instruction") or "",
                    "prompt": "same person, natural casual selfie",
                    "aspect_ratio": "3:4",
                    "used_avatar_reference": True,
                    "duration_ms": 1234.5,
                    "image": {
                        "filename": "ai-generated-selfie.png",
                        "data_url": "data:image/png;base64,omitted-by-dev-proxy",
                        "media_id": "media-1",
                        "url": "/v1/media/media-1",
                        "mime_type": "image/png",
                        "size_bytes": 123,
                        "source": "TOOL_GENERATED_SELFIE",
                    },
                }
            )
        if url.endswith("/v1/characters/mika/avatar/from-chat"):
            return FakeResponse({"character_id": "mika", "avatar_url": "/v1/characters/mika/avatar/asset?v=1"})
        if url.endswith("/health"):
            return FakeResponse({"ok": True})
        return FakeResponse({"detail": "not found"}, status_code=404)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def settings():
    return Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        chat_model="fake-model",
        embedding_provider="deterministic",
        image_generation_timeout_seconds=180,
    )


def test_dev_console_exposes_real_imagegen_controls():
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/dev_visual.js").read_text(encoding="utf-8")
    assert "ImageGen" in html
    assert 'id="imageProvider"' in html
    assert 'id="imagePurpose"' in html
    assert 'id="imageGenPreview"' in html
    assert 'id="rewriteImagePrompt"' in html
    assert 'id="imageRewriteResult"' in html
    assert 'id="useImageAsAvatar"' in html
    assert "/v1/dev/visual/providers" in script
    assert "/v1/dev/imagegen/rewrite" in script
    assert "/v1/dev/imagegen" in script
    assert "/v1/dev/avatar-from-media" in script


def test_dev_visual_proxy_uses_explicit_character_runtime_image_tool():
    fake = FakeVisualHttpClient()
    app = create_dev_app(settings=settings(), http_client=fake, model_factory=lambda _: object())
    with TestClient(app) as client:
        providers = client.get("/v1/dev/visual/providers")
        characters = client.get("/v1/dev/characters")
        rewritten = client.post(
            "/v1/dev/imagegen/rewrite",
            json={
                "character_id": "mika",
                "provider": "agnes",
                "purpose": "SELFIE",
                "visual_intent": "自然自拍",
                "use_avatar_reference": True,
            },
        )
        generated = client.post(
            "/v1/dev/imagegen",
            json={
                "character_id": "mika",
                "provider": "agnes",
                "purpose": "SELFIE",
                "visual_intent": "自然自拍",
                "use_avatar_reference": True,
            },
        )
        avatar = client.post(
            "/v1/dev/avatar-from-media",
            json={"character_id": "mika", "media_id": "media-1"},
        )

    assert providers.status_code == 200
    assert providers.json()["providers"][0]["available"] is True
    assert characters.json()["characters"][0]["id"] == "mika"
    assert rewritten.status_code == 200
    assert rewritten.json()["prompt"] == "same person, natural casual selfie"
    assert generated.status_code == 200
    assert generated.json()["image"]["url"] == "http://127.0.0.1:8000/v1/media/media-1"
    assert "data_url" not in generated.json()["image"]
    assert avatar.status_code == 200

    rewrite_call = next(call for call in fake.calls if call[1].endswith("/v1/characters/mika/images/rewrite"))
    assert rewrite_call[2]["json"]["instruction"] == "自然自拍"
    assert rewrite_call[2]["json"]["purpose"] == "SELFIE"
    assert rewrite_call[2]["json"]["use_avatar_reference"] is True

    image_call = next(call for call in fake.calls if call[1].endswith("/v1/characters/mika/images/generate"))
    assert image_call[2]["json"]["instruction"] == "自然自拍"
    assert image_call[2]["json"]["purpose"] == "SELFIE"
    assert image_call[2]["json"]["use_avatar_reference"] is True
    assert image_call[2]["json"]["persist_result"] is True


def test_chat_ai_image_tool_generates_into_existing_image_draft_instead_of_auto_sending():
    ai_script = Path("src/character_memory/web/ai_images.js").read_text(encoding="utf-8")
    image_script = Path("src/character_memory/web/images.js").read_text(encoding="utf-8")
    index = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")

    assert "/images/rewrite" in ai_script
    assert "/images/generate" in ai_script
    assert "openDataDraft" in ai_script
    assert 'source:"AI_GENERATED"' in ai_script
    assert "CM.sendDirectPayload" not in ai_script
    assert "openDataDraft" in image_script
    assert "来自 AI 生成" in image_script
    assert "/static/ai_images.js" in index
    assert "/static/ai_images.css" in index


def test_generated_media_sse_client_uses_media_asset_route():
    script = Path("src/character_memory/web/visual_client.js").read_text(encoding="utf-8")
    assert "metadata.media_id" in script
    assert 'metadata.action !== "IMAGE"' in script
    assert "/v1/media/" in script
