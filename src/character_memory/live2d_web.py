"""Read-only Live2D model assets for the existing browser call surface.

Operators place trusted, exported Cubism model families in media/live2d/<character_id>.
This is deliberately not a generic upload or remote-URL proxy.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
from urllib.parse import quote

_ALLOWED_SUFFIXES = (".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp")


def model_directory(root: Path, character_id: str) -> Path:
    """A character must not escape the configured media directory."""
    if not character_id or character_id in {".", ".."} or "/" in character_id or "\\" in character_id:
        raise ValueError("invalid character id")
    base = root.resolve()
    candidate = (base / character_id).resolve()
    if candidate.parent != base:
        raise ValueError("invalid character directory")
    return candidate


def model_manifest(root: Path, character_id: str) -> Path | None:
    folder = model_directory(root, character_id)
    if not folder.is_dir():
        return None
    candidates = [folder / "model3.json", *sorted(folder.glob("*.model3.json"))]
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve().parent == folder and candidate.stat().st_size <= 1024 * 1024:
            return candidate
    return None


def resolve_model_asset(root: Path, character_id: str, relative_path: str) -> Path:
    folder = model_directory(root, character_id)
    if (
        not relative_path or relative_path.startswith("/")
        or "\\" in relative_path or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise ValueError("invalid model asset path")
    candidate = (folder / relative_path).resolve()
    if not candidate.is_relative_to(folder) or candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError("invalid model asset path")
    if not candidate.is_file() or candidate.stat().st_size > 64 * 1024 * 1024:
        raise FileNotFoundError(relative_path)
    return candidate


def model_capabilities(root: Path, character_id: str) -> dict | None:
    """Only expose named resources that exist inside this character's model family."""
    try:
        manifest = model_manifest(root, character_id)
        if manifest is None:
            return None
        data = manifest.read_bytes()
        model = json.loads(data)
        if model.get("Version") != 3:
            return None
        refs = model.get("FileReferences", {})
        def exists(item):
            if not isinstance(item, dict) or not isinstance(item.get("File"), str):
                return False
            try:
                return resolve_model_asset(root, character_id, item["File"]).stat().st_size > 0
            except (ValueError, FileNotFoundError):
                return False
        def named(name):
            return isinstance(name, str) and 0 < len(name) <= 64 and not any(ord(c) < 32 for c in name)
        motions = [name for name, items in refs.get("Motions", {}).items() if named(name) and isinstance(items, list) and items and exists(items[0])][:32]
        expressions = [item["Name"] for item in refs.get("Expressions", []) if exists(item) and named(item.get("Name"))][:32]
        return {"revision": hashlib.sha256(data).hexdigest(), "motions": motions, "expressions": expressions}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def attach_live2d_routes(app, root: Path, character_profiles):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    root = Path(root)
    app.state.live2d_root = root

    def ensure_character(character_id: str):
        if not any(item.get("id") == character_id for item in character_profiles()):
            raise HTTPException(status_code=404, detail="Unknown character")

    @app.get("/v1/characters/{character_id}/live2d")
    def live2d_metadata(character_id: str):
        ensure_character(character_id)
        try:
            manifest = model_manifest(root, character_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid character")
        if manifest is None:
            return {"available": False, "model_url": None}
        filename = quote(manifest.name, safe="")
        char_id = quote(character_id, safe="")
        return {
            "available": True,
            "model_url": f"/v1/characters/{char_id}/live2d/files/{filename}",
            "capabilities": model_capabilities(root, character_id),
        }

    @app.get("/v1/characters/{character_id}/live2d/files/{relative_path:path}")
    def live2d_asset(character_id: str, relative_path: str):
        ensure_character(character_id)
        try:
            path = resolve_model_asset(root, character_id, relative_path)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="Model asset not found")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
