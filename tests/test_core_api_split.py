from pathlib import Path
from types import SimpleNamespace

from character_memory.api import (
    CharacterCapacityConfirmationRequired,
    CharacterCapacityExceeded,
    ChatRequest,
    CreateCharacterRequest,
    create_api,
)
from character_memory.api_contracts import (
    CharacterCapacityConfirmationRequired as ContractConfirmationRequired,
    CharacterCapacityExceeded as ContractExceeded,
    ChatRequest as ContractChatRequest,
    CreateCharacterRequest as ContractCreateCharacterRequest,
)
from character_memory.config import Settings
from character_memory.storage.sqlite import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "character_memory"


def test_api_py_is_a_composition_root_not_a_route_bucket():
    api = (SRC / "api.py").read_text(encoding="utf-8")

    assert len(api.splitlines()) < 500
    for decorator in ("@app.get(", "@app.post(", "@app.patch(", "@app.delete("):
        assert decorator not in api

    assert "attach_core_character_routes(app, route_access)" in api
    assert "attach_core_resource_routes(app, route_access)" in api
    assert "attach_core_direct_routes(app, route_access)" in api
    assert "CoreApiRouteAccess(" in api
    assert "CharacterRuntimeAccess(" in api


def test_public_api_contract_imports_remain_compatible_after_split():
    assert ChatRequest is ContractChatRequest
    assert CreateCharacterRequest is ContractCreateCharacterRequest
    assert CharacterCapacityConfirmationRequired is ContractConfirmationRequired
    assert CharacterCapacityExceeded is ContractExceeded


def test_core_route_modules_own_distinct_surfaces():
    character = (SRC / "core_character_web.py").read_text(encoding="utf-8")
    resources = (SRC / "core_resource_web.py").read_text(encoding="utf-8")
    direct = (SRC / "core_direct_web.py").read_text(encoding="utf-8")

    assert '"/v1/characters"' in character
    assert '"/v1/characters/draft"' in character

    assert '"/v1/stickers"' in resources
    assert '"/v1/images"' in resources
    assert '"/v1/media/{media_id}"' in resources

    assert '"/health"' in direct
    assert '"/v1/chat"' in direct
    assert '"/v1/runtime/{character_id}"' in direct


def test_create_api_registers_all_core_route_surfaces(tmp_path):
    persona = tmp_path / "personas" / "rin" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("id: rin\nname: Rin\n", encoding="utf-8")
    settings = Settings(
        db_path=str(tmp_path / "api-split.db"),
        persona_path=str(persona),
        embedding_provider="deterministic",
    )
    store = SQLiteStore(settings.db_path)
    bundle = SimpleNamespace(
        settings=settings,
        store=store,
        characters=[
            {
                "id": "rin",
                "name": "Rin",
                "identity": "",
                "tagline": "",
                "persona_path": str(persona),
            }
        ],
    )
    try:
        app = create_api(bundle=bundle)
        routes = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        for expected in {
            ("/health", "GET"),
            ("/v1/characters", "GET"),
            ("/v1/characters", "POST"),
            ("/v1/stickers", "GET"),
            ("/v1/images", "GET"),
            ("/v1/chat/history", "GET"),
            ("/v1/chat", "POST"),
            ("/v1/runtime/{character_id}", "GET"),
        }:
            assert expected in routes
    finally:
        store.close()
