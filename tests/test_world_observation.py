from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx
import pytest

from character_memory.search import SearchApiProvider
from character_memory.world_observation import SafeWebFetcher, WorldObservationService


def test_safe_web_fetcher_extracts_title_description_thumbnail_and_text(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.org/story"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Story Title</title>"
                '<meta name="description" content="Story summary">'
                '<meta property="og:image" content="/cover.jpg">'
                "</head><body><script>ignore me</script><p>Hello world.</p></body></html>"
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SafeWebFetcher(client=client)
    page = fetcher.fetch("https://example.org/story")

    assert page.title == "Story Title"
    assert page.description == "Story summary"
    assert page.thumbnail_url == "https://example.org/cover.jpg"
    assert "Hello world." in page.content
    assert "ignore me" not in page.content
    client.close()


def test_safe_web_fetcher_rejects_private_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ],
    )
    fetcher = SafeWebFetcher(client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200))))
    with pytest.raises(ValueError, match="non-public"):
        fetcher.fetch("http://localhost/internal")


def test_world_observation_combines_search_and_first_page(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def search_handler(request: httpx.Request) -> httpx.Response:
        assert "engine=google" in str(request.url)
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "Fresh Story",
                        "link": "https://example.org/story",
                        "domain": "example.org",
                        "snippet": "Search summary",
                    }
                ]
            },
        )

    def fetch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Fresh Story Full</title><meta name='description' content='Page summary'><p>Full page body.</p>",
        )

    search_client = httpx.Client(transport=httpx.MockTransport(search_handler))
    fetch_client = httpx.Client(transport=httpx.MockTransport(fetch_handler))
    settings = SimpleNamespace(
        search_api_key="secret",
        search_provider="searchapi",
        search_country="jp",
        search_language="zh-cn",
        search_safe_search="strict",
    )
    service = WorldObservationService(
        settings,
        provider=SearchApiProvider("secret", client=search_client),
        fetcher=SafeWebFetcher(client=fetch_client),
    )

    bundle = service.observe("interesting thing", limit=3)
    assert bundle.query == "interesting thing"
    assert bundle.observations[0].source_type == "WEB_PAGE"
    assert bundle.observations[0].title == "Fresh Story Full"
    assert bundle.observations[0].snippet == "Page summary"
    assert bundle.observations[1].source_type == "WEB_SEARCH"
    assert bundle.observations[1].title == "Fresh Story"

    search_client.close()
    fetch_client.close()
