"""Guard the Android V1 route inventory against accidental path/method drift.

This is a lightweight AST contract check, intentionally runnable without starting
models, SQLite, provider sidecars or Android emulators. Request/response semantics
still require integration fixtures and the runtime FastAPI OpenAPI schema.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "contracts" / "android-v1-route-inventory.json"


def _decorated_routes(filename: Path) -> dict[tuple[str, str], int | None]:
    tree = ast.parse(filename.read_text(encoding="utf-8"), filename=str(filename))
    routes: dict[tuple[str, str], int | None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if not isinstance(dec.func.value, ast.Name) or dec.func.value.id != "app":
                continue
            method = dec.func.attr
            if method not in {"get", "post", "put", "patch", "delete", "websocket"}:
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            route = dec.args[0].value
            if not isinstance(route, str):
                continue
            status = next(
                (
                    kw.value.value
                    for kw in dec.keywords
                    if kw.arg == "status_code" and isinstance(kw.value, ast.Constant)
                ),
                None,
            )
            routes[("WS" if method == "websocket" else method.upper(), route)] = status
    return routes


def test_android_v1_route_manifest_matches_registered_source_routes() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["kind"] == "CharacterMemoryAndroidV1EndpointInventory"
    assert data["routes"], "contract inventory must not silently become empty"
    seen: set[tuple[str, str, str]] = set()
    file_routes: dict[Path, dict[tuple[str, str], int | None]] = {}
    for item in data["routes"]:
        service, method, path = item["service"], item["method"], item["path"]
        assert service in {"CORE", "MEDIA"}
        key = (service, method, path)
        assert key not in seen, f"duplicate client route contract: {key}"
        seen.add(key)
        relative_source = Path(item["source"])
        assert relative_source.parts[:2] == ("src", "character_memory")
        source = ROOT / relative_source
        assert source.is_file(), f"source moved without contract update: {relative_source}"
        found = file_routes.setdefault(source, _decorated_routes(source))
        assert (method, path) in found, (
            f"Android contract drift: {key} was removed, renamed or method-changed in {relative_source}"
        )


def test_async_android_chat_and_visual_acceptance_remains_202() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {
        ("CORE", "POST", "/v1/chat/messages"),
        ("CORE", "POST", "/v1/groups/{conversation_id}/messages"),
        ("CORE", "POST", "/v1/visual/direct/messages"),
        ("CORE", "POST", "/v1/visual/groups/{conversation_id}/messages"),
        ("CORE", "POST", "/v1/visual/direct/observations"),
    }
    observed: set[tuple[str, str, str]] = set()
    for item in data["routes"]:
        key = (item["service"], item["method"], item["path"])
        if key not in expected:
            continue
        route = _decorated_routes(ROOT / item["source"])
        assert route[(item["method"], item["path"])] == 202, (
            f"Android async contract status drift: {key} no longer returns 202"
        )
        observed.add(key)
    assert observed == expected, f"missing V1 async routes: {expected - observed}"
