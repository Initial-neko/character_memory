from __future__ import annotations

import socket

import pytest

from character_memory.browser_web import HeadlessBrowserWebFetcher
from character_memory.search import FetchedPage, ImageSearchResult, SearchProvider, WebSearchResult
from character_memory.world_observation import WorldObservationService


class FakeSearchProvider(SearchProvider):
    def __init__(self):
        self.queries = []

    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        return []

    def search_web(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        self.queries.append((query, limit))
        return [
            WebSearchResult(
                title="First result",
                url="https://one.example/article",
                snippet="first snippet",
                source_domain="one.example",
                published_at="2026-09-22",
            ),
            WebSearchResult(
                title="Second result",
                url="https://two.example/post",
                snippet="second snippet",
                source_domain="two.example",
            ),
            WebSearchResult(
                title="Third result",
                url="https://three.example/post",
                snippet="third snippet",
                source_domain="three.example",
            ),
        ]


class FakeBrowserFetcher:
    def __init__(self):
        self.urls = []

    def fetch_many(self, urls, *, max_chars=12000):
        self.urls = list(urls)
        return (
            [
                FetchedPage(
                    url=urls[0],
                    title="Rendered first page",
                    content="JavaScript rendered article body",
                    content_type="text/html",
                    description="rendered description",
                )
            ],
            [{"url": urls[1], "error": "page unavailable"}] if len(urls) > 1 else [],
        )


def test_world_observation_searches_then_uses_browser_and_keeps_page_failures_local():
    provider = FakeSearchProvider()
    browser = FakeBrowserFetcher()
    service = WorldObservationService(provider, browser)

    result = service.observe(
        "agent memory systems",
        max_pages=2,
        max_chars_per_page=4321,
        search_limit=5,
    )

    assert provider.queries == [("agent memory systems", 5)]
    assert browser.urls == [
        "https://one.example/article",
        "https://two.example/post",
    ]
    assert result["search_results"] == 3
    assert len(result["observations"]) == 1
    observation = result["observations"][0]
    assert observation.title == "Rendered first page"
    assert observation.url == "https://one.example/article"
    assert observation.source_domain == "one.example"
    assert observation.snippet == "first snippet"
    assert observation.content == "JavaScript rendered article body"
    assert observation.published_at == "2026-09-22"
    assert result["errors"] == [
        {"url": "https://two.example/post", "error": "page unavailable"}
    ]


def test_world_observation_hard_caps_rendered_pages():
    provider = FakeSearchProvider()
    browser = FakeBrowserFetcher()
    service = WorldObservationService(provider, browser)

    service.observe("topic", max_pages=1, search_limit=10)

    assert browser.urls == ["https://one.example/article"]


def test_headless_world_browser_rejects_loopback_before_playwright_launch(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ],
    )
    fetcher = HeadlessBrowserWebFetcher(channel="chromium")

    with pytest.raises(ValueError, match="local host|non-public"):
        fetcher.fetch("http://localhost/private")


def test_headless_world_browser_rejects_non_http_schemes():
    fetcher = HeadlessBrowserWebFetcher(channel="chromium")

    with pytest.raises(ValueError, match="absolute http"):
        fetcher.fetch("file:///etc/passwd")
