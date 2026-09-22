from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from character_memory.domain.models import SpaceMediaIntent
from character_memory.media import MediaStorage
from character_memory.remote_media import RemoteMedia
from character_memory.search import ImageSearchResult, SearchApiProvider
from character_memory.space_media_executor import SpaceMediaExecutor
from character_memory.storage.sqlite import SQLiteStore
from character_memory.visual_generation import ImageGenerationResult


PNG = b"\x89PNG\r\n\x1a\nspace-test"
WAV = b"RIFF" + (b"\x00" * 4) + b"WAVEfmt " + (b"\x00" * 24)


class FakeSearchProvider:
    def search_images(self, query: str, *, limit: int = 12):
        return [
            ImageSearchResult(
                title=f"wide-{index}",
                image_url=f"https://images.example.test/{index}.png",
                thumbnail_url=f"https://thumbs.example.test/{index}.png",
                source_page_url=f"https://example.test/page/{index}",
                source_domain="example.test",
                width=2000,
                height=400,
            )
            for index in range(limit)
        ]


class FakeFetcher:
    def fetch_image(self, url: str):
        return RemoteMedia(payload=PNG + url.encode("utf-8"), content_type="image/png", source_url=url)

    def close(self):
        pass


class FailingFetcher(FakeFetcher):
    def fetch_image(self, url: str):
        raise RuntimeError("network unavailable")


class FakeTTSResponse:
    def __init__(self, *, status_code=200, content=WAV, detail=""):
        self.status_code = status_code
        self.content = content
        self.headers = {"x-media-audio-ms": "2450"}
        self.text = detail

    def json(self):
        if not self.text:
            return {}
        return {"detail": self.text}


class FakeTTSClient:
    def __init__(self, response=None):
        self.response = response or FakeTTSResponse()
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        return self.response

    def close(self):
        pass


class FakeVisualModel:
    def _request(self, messages, conversation_id, json_object=False):
        return "cinematic rainy street, natural candid atmosphere"


class FakeImageProvider:
    supports_reference_images = False

    def available(self):
        return True

    def generate(self, request):
        return ImageGenerationResult(
            provider="fake",
            model="fake-image-1",
            payload=PNG,
            mime_type="image/png",
            width=1024,
            height=768,
        )


def _access(tmp_path, *, max_items=3):
    store = SQLiteStore(tmp_path / "space-media-executor.db")
    media_storage = MediaStorage(tmp_path / "media")
    settings = SimpleNamespace(
        space_media_enabled=True,
        space_media_max_items=max_items,
        space_image_search_enabled=True,
        space_image_generation_enabled=True,
        image_generation_provider="fake",
        tts_provider="kokoro",
    )
    access = SimpleNamespace(
        settings=settings,
        read_store=store,
        store=lambda: store,
        media_storage=media_storage,
    )
    return access, store


def test_searchapi_provider_does_not_apply_avatar_shape_filter():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "images": [
                    {
                        "title": "wide landscape",
                        "original": {
                            "link": "https://cdn.example.test/wide.jpg",
                            "width": 2000,
                            "height": 400,
                        },
                        "thumbnail": "https://cdn.example.test/wide-thumb.jpg",
                        "source": {
                            "link": "https://example.test/wide",
                            "name": "example",
                        },
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SearchApiProvider("test-key", client=client)
    try:
        results = provider.search_images("wide landscape", limit=3)
    finally:
        client.close()

    assert len(results) == 1
    assert results[0].width == 2000
    assert results[0].height == 400


def test_space_search_executor_persists_ordered_images_and_respects_cap(tmp_path):
    access, store = _access(tmp_path, max_items=2)
    executor = SpaceMediaExecutor(
        access,
        search_provider=FakeSearchProvider(),
        remote_fetcher=FakeFetcher(),
    )
    intent = SpaceMediaIntent(type="SEARCH_IMAGE", query="Tokyo night", count=5)

    result = executor.execute(
        "c00",
        [intent],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert result["errors"] == []
    assert len(result["relations"]) == 2
    assert [item["source_type"] for item in result["relations"]] == ["SEARCH", "SEARCH"]
    assert all(item["metadata"]["width"] == 2000 for item in result["relations"])
    for relation in result["relations"]:
        asset = store.get_media_asset(relation["media_id"])
        assert asset is not None
        assert access.media_storage.asset_path(asset) is not None
        assert asset.source == "SPACE_SEARCH_IMAGE"
    store.close()


def test_space_media_executor_is_fail_soft_per_intent(tmp_path):
    access, store = _access(tmp_path)
    executor = SpaceMediaExecutor(
        access,
        search_provider=FakeSearchProvider(),
        remote_fetcher=FailingFetcher(),
    )

    result = executor.execute(
        "c00",
        [SpaceMediaIntent(type="SEARCH_IMAGE", query="unavailable image", count=1)],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert result["relations"] == []
    assert result["errors"][0]["type"] == "SEARCH_IMAGE"
    assert "network unavailable" in result["errors"][0]["error"]
    store.close()


def test_space_generated_image_uses_existing_visual_provider_and_media_store(tmp_path):
    access, store = _access(tmp_path)
    runtime = SimpleNamespace(
        store=store,
        model=FakeVisualModel(),
        persona="A persistent fictional character with stable visual identity.",
    )
    executor = SpaceMediaExecutor(
        access,
        image_providers={"fake": FakeImageProvider()},
    )

    result = executor.execute(
        "c00",
        [
            SpaceMediaIntent(
                type="GENERATE_IMAGE",
                purpose="SCENE",
                visual_intent="雨夜街道的安静氛围",
                count=1,
            )
        ],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
        runtime=runtime,
    )

    assert result["errors"] == []
    assert len(result["relations"]) == 1
    relation = result["relations"][0]
    assert relation["source_type"] == "GENERATED"
    assert relation["metadata"]["provider"] == "fake"
    assert relation["metadata"]["purpose"] == "SCENE"
    asset = store.get_media_asset(relation["media_id"])
    assert asset is not None
    assert asset.source == "SPACE_GENERATED_SCENE"
    assert access.media_storage.asset_path(asset) is not None
    store.close()


def test_space_voice_uses_formal_tts_and_persists_transcript_metadata(tmp_path):
    access, store = _access(tmp_path)
    client = FakeTTSClient()
    executor = SpaceMediaExecutor(
        access,
        tts_client=client,
        media_base="http://127.0.0.1:8001",
    )

    result = executor.execute(
        "c00",
        [SpaceMediaIntent(type="VOICE", voice_text="今天其实有点想偷懒，就直接说啦。")],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert result["errors"] == []
    assert client.calls == [
        (
            "http://127.0.0.1:8001/v1/tts",
            {"text": "今天其实有点想偷懒，就直接说啦。", "voice": "c00"},
        )
    ]
    assert len(result["relations"]) == 1
    relation = result["relations"][0]
    assert relation["media_type"] == "VOICE"
    assert relation["source_type"] == "GENERATED"
    assert relation["metadata"]["transcript"] == "今天其实有点想偷懒，就直接说啦。"
    assert relation["metadata"]["duration_ms"] == 2450
    assert relation["metadata"]["provider"] == "kokoro"
    asset = store.get_media_asset(relation["media_id"])
    assert asset is not None
    assert asset.source == "SPACE_VOICE"
    assert asset.mime_type == "audio/wav"
    assert access.media_storage.asset_path(asset) is not None
    store.close()


def test_space_voice_failure_is_fail_soft_and_does_not_create_asset(tmp_path):
    access, store = _access(tmp_path)
    client = FakeTTSClient(FakeTTSResponse(status_code=503, content=b"", detail="tts unavailable"))
    executor = SpaceMediaExecutor(
        access,
        tts_client=client,
        media_base="http://127.0.0.1:8001",
    )

    result = executor.execute(
        "c00",
        [SpaceMediaIntent(type="VOICE", voice_text="这次应该失败。")],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert result["relations"] == []
    assert result["errors"][0]["type"] == "VOICE"
    assert "tts unavailable" in result["errors"][0]["error"]
    assert store.conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    store.close()
