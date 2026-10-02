"""The local (keyless) web-search provider, and where it is allowed to be wired.

Two things must not drift:

* the provider answers public-web discovery only -- it has no image search, so
  it must never be handed to Avatar/Space image discovery;
* it pins an engine set. The library default looks harmless and is not: rank
  fusion waits for the slowest engine, and the default set contains one engine
  that burns its whole timeout and one that answers 403.
"""

from __future__ import annotations

from types import SimpleNamespace

from character_memory.config import Settings
from character_memory.runtime_services import build_runtime_services
from character_memory.search import LocalSearchProvider


def _hit(title: str, url: str, snippet: str, host: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, url=url, snippet=snippet, host=host, score=1.0, engines=["duckduckgo"])


def test_local_search_maps_provider_hits_onto_the_shared_result_shape(monkeypatch):
    calls: list[dict] = []

    def fake_search_sync(query, *, limit, engines, timeout):
        calls.append({"query": query, "limit": limit, "engines": engines, "timeout": timeout})
        return SimpleNamespace(
            hits=[
                _hit("东京咖啡店", "https://example.com/cafe", "20 间必去", "example.com"),
                _hit("", "https://news.example.org/a", "", ""),
                _hit("", "", "no url", ""),
            ],
            failed={"mojeek": "timed out"},
        )

    monkeypatch.setattr("character_memory.search.search_sync", fake_search_sync)

    results = LocalSearchProvider(timeout_seconds=9.0).search_web("东京 咖啡店", limit=5)

    assert calls == [
        {
            "query": "东京 咖啡店",
            "limit": 5,
            "engines": LocalSearchProvider.ENGINES,
            "timeout": 9.0,
        }
    ]
    # The hit without a url is dropped rather than surfacing an unusable link.
    assert [item.url for item in results] == ["https://example.com/cafe", "https://news.example.org/a"]
    assert results[0].source_domain == "example.com"
    assert results[1].source_domain == "news.example.org"
    # Engine result pages carry no reliable publication date.
    assert [item.published_at for item in results] == [None, None]


def test_local_search_pins_engines_instead_of_using_the_library_default():
    from webless import BingEngine, DuckDuckGoEngine, GENERAL_ENGINES

    assert set(LocalSearchProvider.ENGINES) == {DuckDuckGoEngine, BingEngine}
    assert set(LocalSearchProvider.ENGINES) != set(GENERAL_ENGINES), (
        "the general engine set makes every call wait out a 12s timeout"
    )


def test_local_search_refuses_image_discovery_rather_than_returning_nothing():
    """A silent empty list would look like 'no results' instead of 'wrong provider'."""

    import pytest

    with pytest.raises(RuntimeError, match="does not provide image search"):
        LocalSearchProvider().search_images("cat portrait")


def test_local_web_search_swaps_only_the_web_half_of_the_composition(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "local-web-search.db"),
        search_provider="searchapi",
        search_api_key="",
        web_search_provider="local",
        image_generation_provider="agnes",
    )
    services = build_runtime_services(settings)
    try:
        assert isinstance(services.web_search_provider, LocalSearchProvider)
        assert services.world_observer.search_provider is services.web_search_provider
        # Image discovery keeps the API provider: the local one has no images.
        assert services.avatar_search.provider is services.search_provider
        assert not isinstance(services.avatar_search.provider, LocalSearchProvider)
    finally:
        services.close()


def test_auto_resolves_to_one_shared_provider_that_is_closed_once(tmp_path):
    """``auto`` aliases search_provider, so close() must not close it twice."""

    settings = Settings(
        db_path=str(tmp_path / "auto-web-search.db"),
        search_provider="searchapi",
        search_api_key="",
        image_generation_provider="agnes",
    )
    services = build_runtime_services(settings)
    assert services.web_search_provider is services.search_provider

    closes: list[int] = []
    original = services.search_provider.close

    def counting_close():
        closes.append(1)
        original()

    services.search_provider.close = counting_close
    services.close()

    assert len(closes) == 1


def test_world_status_reports_the_provider_that_actually_serves_discovery(tmp_path):
    """A status payload that echoed only the image provider would describe a
    discovery path that is not the one running."""

    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from character_memory.storage.sqlite import SQLiteStore
    from character_memory.background_services import BackgroundServices
    from character_memory.world_web import attach_world_routes

    db_path = str(tmp_path / "world-status.db")
    settings = Settings(
        db_path=db_path,
        search_provider="searchapi",
        search_api_key="",
        web_search_provider="local",
    )
    services = build_runtime_services(settings)
    store = SQLiteStore(db_path)
    app = FastAPI()
    app.state.character_memory = SimpleNamespace(settings=settings, services=services, read_store=store)
    app.state.background_services = BackgroundServices()
    try:
        attach_world_routes(app)
        with TestClient(app) as client:
            payload = client.get("/v1/world/status").json()
    finally:
        services.close()
        store.close()

    assert payload["web_search_provider"] == "local"
    assert payload["web_search_effective"] == "LocalSearchProvider"
    # The local provider consumes no key, so the web path is usable while
    # search_api_key stays empty.
    assert payload["web_search_configured"] is True
    assert payload["search_configured"] is False
