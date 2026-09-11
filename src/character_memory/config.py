from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    api_key: str = ""
    base_url: str = "https://opencode.ai/zen/go/v1"
    chat_model: str = "deepseek-flash"
    # Optional compatibility override for providers that still require a
    # dedicated vision model. Empty/None means image turns reuse chat_model.
    vision_model: str | None = None
    chat_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_attempts: int = Field(default=2, ge=1, le=4)

    embedding_provider: str = "sentence-transformers"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    # Search is deliberately separate from the LLM runtime. P0.15 only uses
    # image search for avatar discovery; web_search/web_fetch remain reserved.
    search_provider: str = "brave"
    search_api_key: str = ""
    search_country: str = "ALL"
    search_language: str = "zh"
    search_safe_search: str = "strict"

    db_path: str = "data/character-memory.db"
    media_dir: str = ""
    media_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    # User-imported stickers are account/application resources, not character-owned.
    # Empty means <db parent>/stickers.
    sticker_dir: str = ""
    # Empty means <db parent>/avatars. Selected web avatars are downloaded here
    # so chat UI never depends on a third-party hotlink remaining alive.
    avatar_dir: str = ""
    avatar_max_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    persona_path: str = "personas/rin/persona.yaml"
    recall_limit: int = Field(default=8, ge=1, le=32)


def load_settings(path: str = "config.yaml") -> Settings:
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data["api_key"] = os.getenv("OPENCODE_GO_API_KEY", data.get("api_key", ""))
    # config.yaml is the primary declaration for search credentials. The env
    # override is optional for deployments that prefer secret injection.
    data["search_api_key"] = os.getenv("BRAVE_SEARCH_API_KEY", data.get("search_api_key", ""))
    data["db_path"] = os.getenv("CHARACTER_MEMORY_DB_PATH", data.get("db_path", "data/character-memory.db"))
    return Settings.model_validate(data)


def resolve_media_dir(settings: Settings) -> Path:
    configured = str(getattr(settings, "media_dir", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.db_path).parent / "media"


def resolve_sticker_dir(settings: Settings) -> Path:
    configured = str(getattr(settings, "sticker_dir", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.db_path).parent / "stickers"


def resolve_avatar_dir(settings: Settings) -> Path:
    configured = str(getattr(settings, "avatar_dir", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.db_path).parent / "avatars"


def _avatar_url(settings: Settings, character_id: str) -> str:
    directory = resolve_avatar_dir(settings) / character_id
    for extension in (".jpg", ".png", ".gif", ".webp"):
        path = directory / f"avatar{extension}"
        if path.is_file():
            return f"/v1/characters/{quote(character_id, safe='')}/avatar/asset?v={path.stat().st_mtime_ns}"
    return ""


def load_persona(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _persona_root(settings: Settings) -> Path:
    configured = Path(settings.persona_path)
    if configured.name == "persona.yaml" and configured.parent.parent != configured.parent:
        return configured.parent.parent
    return Path("personas")


def discover_character_profiles(settings: Settings) -> list[dict[str, str]]:
    """Discover characters directly from personas/*/persona.yaml.

    Persona files stay the single character definition source. The UI/API does
    not need a second character registry or duplicated config list. Avatar state
    is an asset concern and is projected into the public profile dynamically.
    """

    root = _persona_root(settings)
    paths = sorted(root.glob("*/persona.yaml")) if root.exists() else []
    configured = Path(settings.persona_path)
    if configured.exists() and configured not in paths:
        paths.insert(0, configured)

    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        character_id = str(data.get("id") or path.parent.name).strip()
        if not character_id or character_id in seen:
            continue
        seen.add(character_id)
        profiles.append(
            {
                "id": character_id,
                "name": str(data.get("name") or character_id),
                "identity": str(data.get("identity") or ""),
                "tagline": str(data.get("tagline") or ""),
                "avatar_url": _avatar_url(settings, character_id),
                "persona_path": str(path),
            }
        )

    if not profiles:
        profiles.append(
            {
                "id": "rin",
                "name": "Rin",
                "identity": "",
                "tagline": "",
                "avatar_url": _avatar_url(settings, "rin"),
                "persona_path": settings.persona_path,
            }
        )
    return profiles


def resolve_persona_path(settings: Settings, character_id: str) -> str:
    for profile in discover_character_profiles(settings):
        if profile["id"] == character_id:
            return profile["persona_path"]
    known = ", ".join(profile["id"] for profile in discover_character_profiles(settings))
    raise KeyError(f"unknown character_id={character_id!r}; known={known}")
