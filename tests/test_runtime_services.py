from pathlib import Path

from character_memory.config import Settings
from character_memory.runtime_services import build_runtime_services


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_services_share_search_and_world_infrastructure(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "runtime-services.db"),
        search_provider="searchapi",
        search_api_key="",
        image_generation_provider="agnes",
    )
    services = build_runtime_services(settings)
    try:
        assert services.avatar_search.provider is services.search_provider
        assert services.world_observer.search_provider is services.search_provider
        assert services.world_observer.web_fetcher is services.world_fetcher
        assert services.avatar_store.root == tmp_path / "avatars"
        assert set(services.image_generation_providers) == {"agnes", "msimg"}
    finally:
        services.close()


def test_feature_routes_consume_composed_services_instead_of_other_routes():
    avatar = (ROOT / "src" / "character_memory" / "avatar_web.py").read_text(encoding="utf-8")
    visual = (ROOT / "src" / "character_memory" / "visual_web.py").read_text(encoding="utf-8")
    world = (ROOT / "src" / "character_memory" / "world_web.py").read_text(encoding="utf-8")
    space_media = (ROOT / "src" / "character_memory" / "space_media_executor.py").read_text(encoding="utf-8")
    api = (ROOT / "src" / "character_memory" / "api.py").read_text(encoding="utf-8")
    server = (ROOT / "src" / "character_memory" / "server.py").read_text(encoding="utf-8")

    assert "build_runtime_services(settings)" in api
    assert "services=services" in api

    assert "services = access.services" in avatar
    assert "SearchApiProvider(" not in avatar
    assert "BraveSearchProvider(" not in avatar

    assert "services = access.services" in visual
    assert "build_image_providers(settings)" not in visual
    assert "attach_avatar_routes() must run before attach_visual_routes()" not in visual

    assert "services = access.services" in world
    assert "avatar_search" not in world
    assert "HeadlessBrowserWebFetcher(" not in world

    assert 'getattr(self.access, "avatar_search"' not in space_media
    assert 'getattr(services, "search_provider"' in space_media

    # Deliberately attach World before Avatar: route order is no longer the DI mechanism.
    assert server.index("attach_world_routes(app)") < server.index("attach_avatar_routes(app)")
