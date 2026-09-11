from __future__ import annotations

import socket

import httpx
import pytest

from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.config import Settings, discover_character_profiles
from character_memory.search import BraveSearchProvider, ImageSearchResult, SearchProvider


def test_brave_image_search_parses_ranked_candidates_without_web_search():
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
                    },
                    {
                        "title": "banner should be filtered",
                        "url": "https://example.org/banner",
                        "source": "example.org",
                        "thumbnail": {"src": "https://imgs.example.org/banner-thumb.jpg"},
                        "properties": {"url": "https://imgs.example.org/banner.jpg", "width": 2000, "height": 200},
                    },
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
    assert results[0].source_page_url == "https://example.org/source"
    assert results[0].width == 900
    assert results[0].height == 900
    assert seen["token"] == "secret"
    assert "safesearch=strict" in seen["url"]
    assert "search_lang=zh" in seen["url"]
    with pytest.raises(NotImplementedError):
        provider.search_web("anything")
    client.close()


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
        return httpx.Response(200, content=b"fake-png", headers={"content-type": "image/png"})

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
    assert (tmp_path / "avatars" / "mika" / "avatar.png").read_bytes() == b"fake-png"
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
