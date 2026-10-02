from __future__ import annotations

import socket

import pytest

from character_memory.browser_web import HeadlessBrowserWebFetcher
from character_memory.search import FetchedPage, ImageSearchResult, SearchProvider, WebSearchResult
from character_memory.world_observation import EXTRA_FETCH_ATTEMPTS, WorldObservationService


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
    """Renders the first URL of each batch and fails the rest.

    Attempts accumulate across batches rather than overwrite: when the top
    candidate is unreachable, discovery opens more pages, and a fake that kept
    only the last batch could not tell that apart from opening one page.
    """

    def __init__(self):
        self.urls = []
        self.batches = []

    def fetch_many(self, urls, *, max_chars=12000):
        batch = list(urls)
        self.batches.append(batch)
        self.urls.extend(batch)
        pages: list[FetchedPage] = []
        errors: list[dict[str, str]] = []
        for index, url in enumerate(batch):
            if index == 0:
                pages.append(
                    FetchedPage(
                        url=url,
                        title="Rendered first page",
                        content="JavaScript rendered article body",
                        content_type="text/html",
                        description="rendered description",
                    )
                )
            else:
                errors.append({"url": url, "error": "page unavailable"})
        return pages, errors


class AllReachableFetcher(FakeBrowserFetcher):
    """Renders every candidate it is handed."""

    def fetch_many(self, urls, *, max_chars=12000):
        batch = list(urls)
        self.batches.append(batch)
        self.urls.extend(batch)
        return (
            [
                FetchedPage(url=url, title="ok", content="body", content_type="text/html")
                for url in batch
            ],
            [],
        )


class UnreachableTopFetcher(FakeBrowserFetcher):
    """Fails every host except the ones named, to stand in for this network.

    A blocked or 5xx host takes its slot with it; ranking order is not
    reachability order, which is why the top hits alone are not enough.
    """

    def __init__(self, reachable: set[str]):
        super().__init__()
        self.reachable = reachable

    def fetch_many(self, urls, *, max_chars=12000):
        batch = list(urls)
        self.batches.append(batch)
        self.urls.extend(batch)
        pages: list[FetchedPage] = []
        errors: list[dict[str, str]] = []
        for url in batch:
            if url in self.reachable:
                pages.append(
                    FetchedPage(
                        url=url,
                        title="Rendered page",
                        content="JavaScript rendered article body",
                        content_type="text/html",
                        description="rendered description",
                    )
                )
            else:
                errors.append({"url": url, "error": "navigation failed"})
        return pages, errors


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
    # The second candidate fails, so the third is promoted to fill the slot that
    # would otherwise have been lost, and ``two``'s failure stays visible.
    assert browser.urls == [
        "https://one.example/article",
        "https://two.example/post",
        "https://three.example/post",
    ]
    assert result["search_results"] == 3
    assert [item.url for item in result["observations"]] == [
        "https://one.example/article",
        "https://three.example/post",
    ]
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


def test_world_observation_renders_in_one_batch_when_the_top_hits_work():
    """The common case must not pay for the fallback: one batch, no extra opens."""

    browser = AllReachableFetcher()
    service = WorldObservationService(FakeSearchProvider(), browser)

    result = service.observe("topic", max_pages=2, search_limit=5)

    assert len(browser.batches) == 1
    assert browser.urls == ["https://one.example/article", "https://two.example/post"]
    assert len(result["observations"]) == 2


def test_world_observation_promotes_past_unreachable_top_hits():
    """Reproduces the live group-creation failure.

    The ranking put two hosts this network cannot open at the top; with a single
    batch of three the whole observation came back empty and group creation
    failed as "no readable public material", identically on every retry.
    """

    provider = FakeSearchProvider()
    browser = UnreachableTopFetcher(reachable={"https://three.example/post"})
    service = WorldObservationService(provider, browser)

    result = service.observe("topic", max_pages=1, search_limit=5)

    # One slot wanted: it walks the ranking until something renders, instead of
    # reporting "no readable public material" after the first unreachable hit.
    assert browser.urls == [
        "https://one.example/article",
        "https://two.example/post",
        "https://three.example/post",
    ]
    assert [item.url for item in result["observations"]] == ["https://three.example/post"]


def test_world_observation_spends_one_slot_per_domain_first():
    """Two slots spent on one host spend an attempt the next source needed."""

    class RepeatedDomainProvider(SearchProvider):
        def search_images(self, query, *, limit=12):
            return []

        def search_web(self, query, *, limit=5):
            return [
                WebSearchResult(
                    title="a", url="https://same.example/1", snippet="", source_domain="same.example"
                ),
                WebSearchResult(
                    title="b", url="https://same.example/2", snippet="", source_domain="same.example"
                ),
                WebSearchResult(
                    title="c", url="https://other.example/1", snippet="", source_domain="other.example"
                ),
            ]

    browser = AllReachableFetcher()
    service = WorldObservationService(RepeatedDomainProvider(), browser)

    service.observe("topic", max_pages=2, search_limit=5)

    # The first hit of each domain is tried before the duplicate is revisited.
    assert browser.urls == ["https://same.example/1", "https://other.example/1"]


def test_world_observation_bounds_how_many_pages_it_will_open():
    """A dead pool must not turn into an unbounded number of browser timeouts."""

    provider = FakeSearchProvider()
    browser = UnreachableTopFetcher(reachable=set())
    service = WorldObservationService(provider, browser)

    result = service.observe("topic", max_pages=2, search_limit=10)

    assert result["observations"] == []
    assert len(browser.urls) <= 2 + EXTRA_FETCH_ATTEMPTS
    assert len(browser.urls) == len(set(browser.urls)), "no candidate is opened twice"


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
