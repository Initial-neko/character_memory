from __future__ import annotations

import socket

import httpx
import pytest

from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.config import Settings, discover_character_profiles
from character_memory.search import BraveSearchProvider, ImageSearchResult, SearchApiProvider, SearchProvider


def test_searchapi_image_search_parses_candidates_and_maps_safe_search():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "images": [
                    {
                        "position": 1,
                        "title": "Mika portrait",
                        "source": {"name": "Example", "link": "https://example.org/source"},
                        "original": {
                            "link": "https://imgs.example.org/full.jpg",
                            "width": 900,
                            "height": 900,
                        },
                        "thumbnail": "https://imgs.example.org/thumb.jpg",
                    },
                    {
                        "position": 2,
                        "title": "banner should be filtered",
                        "source": {"name": "Example", "link": "https://example.org/banner"},
                        "original": {
                            "link": "https://imgs.example.org/banner.jpg",
                            "width": 2000,
                            "height": 200,
                        },
                        "thumbnail": "https://imgs.example.org/banner-thumb.jpg",
                    },
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SearchApiProvider(
        "secret",
        country="jp",
        language="zh-cn",
        safe_search="strict",
        client=client,
    )
    results = provider.search_images("Mika avatar", limit=12)

    # The provider is composition-neutral. Avatar-specific aspect filtering is
    # applied later by AvatarSearchService so Space can reuse wide imagery.
    assert len(results) == 2
    assert results[0].image_url == "https://imgs.example.org/full.jpg"
    assert results[0].thumbnail_url == "https://imgs.example.org/thumb.jpg"
    assert results[0].source_page_url == "https://example.org/source"
    assert results[0].source_domain == "example.org"
    assert results[0].width == 900
    assert results[0].height == 900
    assert results[1].image_url == "https://imgs.example.org/banner.jpg"
    assert results[1].width == 2000
    assert results[1].height == 200
    assert seen["authorization"] == "Bearer secret"
    assert "engine=google_images" in seen["url"]
    assert "gl=jp" in seen["url"]
    assert "hl=zh-cn" in seen["url"]
    assert "safe=active" in seen["url"]
    assert "secret" not in seen["url"]
    client.close()


def test_searchapi_web_search_parses_public_results():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "Rendered web result",
                        "link": "https://news.example.org/story",
                        "snippet": "A short public search snippet.",
                        "date": "Sep 22, 2026",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SearchApiProvider(
        "secret",
        country="sg",
        language="en",
        safe_search="strict",
        client=client,
    )
    try:
        results = provider.search_web("agent memory research", limit=3)
    finally:
        client.close()

    assert len(results) == 1
    assert results[0].title == "Rendered web result"
    assert results[0].url == "https://news.example.org/story"
    assert results[0].snippet == "A short public search snippet."
    assert results[0].source_domain == "news.example.org"
    assert results[0].published_at == "Sep 22, 2026"
    assert seen["authorization"] == "Bearer secret"
    assert "engine=google" in seen["url"]
    assert "gl=sg" in seen["url"]
    assert "hl=en" in seen["url"]
    assert "safe=active" in seen["url"]


def test_searchapi_accepts_legacy_all_country_and_zh_language():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"images": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SearchApiProvider(
        "secret",
        country="ALL",
        language="zh",
        safe_search="strict",
        client=client,
    )
    assert provider.search_images("avatar") == []
    assert "gl=" not in seen["url"]
    assert "hl=zh-cn" in seen["url"]
    client.close()


def test_brave_image_search_remains_available_as_fallback():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-subscription-token")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Mika portrait",
                        "url": "https://example.org/source",
                        "source": "example.org",
                        "confidence": "high",
                        "thumbnail": {"src": "https://imgs.example.org/thumb.jpg", "width": 500, "height": 500},
                        "properties": {"url": "https://imgs.example.org/full.jpg", "width": 900, "height": 900},
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BraveSearchProvider(
        "secret",
        country="ALL",
        language="zh",
        safe_search="strict",
        client=client,
    )
    results = provider.search_images("Mika avatar", limit=12)

    assert len(results) == 1
    assert results[0].image_url == "https://imgs.example.org/full.jpg"
    assert seen["token"] == "secret"
    assert "safesearch=strict" in seen["url"]
    assert "search_lang=zh" in seen["url"]
    client.close()


def test_brave_web_search_parses_public_results():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-subscription-token")
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave result",
                            "url": "https://example.net/article",
                            "description": "Public result description.",
                            "age": "2 hours ago",
                        }
                    ]
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BraveSearchProvider(
        "secret",
        country="SG",
        language="en",
        safe_search="strict",
        client=client,
    )
    try:
        results = provider.search_web("character agents", limit=2)
    finally:
        client.close()

    assert len(results) == 1
    assert results[0].title == "Brave result"
    assert results[0].source_domain == "example.net"
    assert results[0].published_at == "2 hours ago"
    assert seen["token"] == "secret"
    assert "/res/v1/web/search" in seen["url"]


class MixedShapeSearchProvider(SearchProvider):
    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        return [
            ImageSearchResult(
                title="avatar",
                image_url="https://cdn.example.org/avatar.png",
                thumbnail_url="https://cdn.example.org/avatar-thumb.png",
                source_page_url="https://example.org/avatar",
                source_domain="example.org",
                width=512,
                height=512,
            ),
            ImageSearchResult(
                title="wide banner",
                image_url="https://cdn.example.org/banner.png",
                thumbnail_url="https://cdn.example.org/banner-thumb.png",
                source_page_url="https://example.org/banner",
                source_domain="example.org",
                width=2000,
                height=200,
            ),
        ]


def test_avatar_search_service_keeps_avatar_shape_filter_at_product_boundary(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    store = AvatarStore(tmp_path / "avatars", client=client)
    service = AvatarSearchService(MixedShapeSearchProvider(), store)

    search = service.search("mika", "Mika avatar", limit=6)

    assert len(search["candidates"]) == 1
    assert search["candidates"][0]["title"] == "avatar"
    service.close()


class FakeSearchProvider(SearchProvider):
    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        return [
            ImageSearchResult(
                title="candidate",
                image_url="https://cdn.example.org/avatar.png",
                thumbnail_url="https://cdn.example.org/thumb.png",
                source_page_url="https://example.org/page",
                source_domain="example.org",
                width=512,
                height=512,
            )
        ]


def test_avatar_selection_requires_cached_candidate_and_persists_locally(tmp_path, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    def download(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cdn.example.org/avatar.png"
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nvalid-avatar",
            headers={"content-type": "image/png"},
        )

    client = httpx.Client(transport=httpx.MockTransport(download))
    store = AvatarStore(tmp_path / "avatars", client=client)
    service = AvatarSearchService(FakeSearchProvider(), store)

    search = service.search("mika", "Mika portrait", limit=6)
    assert len(search["candidates"]) == 1
    candidate_id = search["candidates"][0]["id"]

    with pytest.raises(ValueError, match="not part of this search"):
        service.select("mika", search["search_id"], "invented")

    metadata = service.select("mika", search["search_id"], candidate_id)
    assert metadata.character_id == "mika"
    assert metadata.search_query == "Mika portrait"
    assert metadata.source_domain == "example.org"
    assert (tmp_path / "avatars" / "mika" / "avatar.png").read_bytes() == b"\x89PNG\r\n\x1a\nvalid-avatar"
    assert store.load("mika") is not None
    assert store.asset_path("mika") is not None
    client.close()


def test_character_profile_projects_local_avatar_url(tmp_path):
    persona = tmp_path / "personas" / "mika" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("id: mika\nname: Mika\nidentity: tester\ntagline: hello\n", encoding="utf-8")
    avatar = tmp_path / "avatars" / "mika" / "avatar.webp"
    avatar.parent.mkdir(parents=True)
    avatar.write_bytes(b"webp")

    settings = Settings(
        persona_path=str(persona),
        db_path=str(tmp_path / "data" / "character-memory.db"),
        avatar_dir=str(tmp_path / "avatars"),
    )
    profiles = discover_character_profiles(settings)

    assert len(profiles) == 1
    assert profiles[0]["id"] == "mika"
    assert profiles[0]["avatar_url"].startswith("/v1/characters/mika/avatar/asset?v=")
