from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field

from character_memory.envfile import effective_env_value
from character_memory.tts_registry import FORMAL_TTS_PROVIDER_PATTERN


# Single source of truth for the voice-call endpointing default. The Media
# Runtime serves this to the browser, and it also falls back to it when a caller
# stubs settings with an object that predates the field.
DEFAULT_VOICE_SILENCE_MS = 900


class Settings(BaseModel):
    # Secret fields remain in the runtime model for backward compatibility, but
    # Settings Center migrates their persisted values out of config.yaml and into
    # the project-local .env file.
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

    # Formal real-time chat TTS selection. Qwen3-TTS is intentionally excluded from
    # this enum; it remains an experimental/future voice-design tool, not a realtime provider.
    tts_provider: str = Field(default="kokoro", pattern=FORMAL_TTS_PROVIDER_PATTERN)
    tts_voice: str = "zf_001"
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_device: str = Field(default="cpu", pattern=r"^(cpu|cuda)$")

    # Voice-call endpointing. The browser closes an utterance after this much
    # silence, so this single number decides where one spoken sentence stops and
    # the next begins. Too short and a normal pause between clauses ships the
    # first half as its own chat turn; too long and the reply feels sluggish.
    # 450 ms was the old hardcoded value and cut people off mid-sentence.
    voice_silence_ms: int = Field(default=DEFAULT_VOICE_SILENCE_MS, ge=200, le=3000)

    # Search is deliberately separate from the LLM runtime. Avatar discovery
    # uses image search only; web_search/web_fetch remain reserved.
    search_provider: str = "searchapi"
    search_api_key: str = ""
    search_country: str = "jp"
    search_language: str = "zh-cn"
    search_safe_search: str = "strict"

    # P0.19 direct-character visual capability. Both providers share one
    # provider-neutral request/result contract. Agnes supports reference images;
    # msimg 0.0.4 is currently text-to-image only in this integration.
    image_generation_provider: str = "agnes"
    image_generation_timeout_seconds: float = Field(default=180.0, ge=10.0, le=600.0)
    agnes_api_key: str = ""
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_image_model: str = "agnes-image-2.1-flash"
    msimg_api_key: str = ""
    msimg_models: str = "qwen"

    db_path: str = "data/character-memory.db"
    media_dir: str = ""
    media_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    # User-imported stickers are account/application resources, not character-owned.
    # Empty means <db parent>/stickers.
    sticker_dir: str = ""
    # Empty means <db parent>/avatars. Selected web/generated/chat avatars are
    # copied here so current avatar state never depends on another asset staying alive.
    avatar_dir: str = ""
    avatar_max_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    persona_path: str = "personas/rin/persona.yaml"
    recall_limit: int = Field(default=8, ge=1, le=32)

    # P0.17 direct-character wake. This is intentionally process-local scheduling:
    # every eligible character gets a TIME_TICK opportunity roughly once per hour.
    # It is not a durable background-job system and does not wake group chats.
    proactive_wake_enabled: bool = True
    proactive_wake_minutes: float = Field(default=60.0, ge=1.0, le=1440.0)

    # Character Space autonomous social life. These are deliberately ordinary
    # persisted settings so the test-stage behavior can be tuned from Settings
    # Center without editing source code.
    space_autonomy_enabled: bool = True
    # Interval between autonomous Space opportunities for one active character.
    # 1440 keeps the production default at roughly once per 24 hours; shorter
    # values are intentionally supported for test soak runs.
    space_opportunity_interval_minutes: float = Field(default=1440.0, ge=10.0, le=10080.0)
    # Upper bound on published Space posts per character per local day. The
    # interval already paces opportunities; this is the explicit ceiling a test
    # operator can raise or remove without touching the interval. 0 means no
    # ceiling. It never forces a post -- a character that decides not to publish
    # simply does not consume the budget.
    space_max_posts_per_day: int = Field(default=0, ge=0, le=200)
    space_audience_size: int = Field(default=5, ge=0, le=10)
    space_scheduler_poll_seconds: float = Field(default=60.0, ge=10.0, le=3600.0)

    # Autonomous Space world/media behavior. These probabilities are gates for
    # whether a capability is even offered to the character on one opportunity;
    # the character may still decide not to use it.
    space_observation_enabled: bool = True
    space_image_search_enabled: bool = True
    space_image_generation_enabled: bool = True
    space_voice_post_enabled: bool = True
    space_link_preview_enabled: bool = True
    space_observation_chance: float = Field(default=0.30, ge=0.0, le=1.0)
    space_media_chance: float = Field(default=0.40, ge=0.0, le=1.0)
    space_voice_chance: float = Field(default=0.15, ge=0.0, le=1.0)
    space_max_images_per_post: int = Field(default=9, ge=1, le=9)

    # Legacy V2 fields remain loadable so existing config.yaml files do not need
    # a destructive migration. The interval scheduler no longer consumes them.
    space_daily_window_start_hour: int = Field(default=18, ge=0, le=23)
    space_daily_window_end_hour: int = Field(default=22, ge=1, le=24)


def load_settings(path: str = "config.yaml") -> Settings:
    p = Path(path)
    env_path = p.resolve().parent / ".env"

    data: dict = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # Secrets resolve without mutating os.environ:
    # system environment > adjacent .env > legacy config field.
    data["api_key"] = effective_env_value("OPENCODE_GO_API_KEY", env_path, str(data.get("api_key", "") or ""))
    data["embedding_api_key"] = effective_env_value(
        "EMBEDDING_API_KEY",
        env_path,
        str(data.get("embedding_api_key", "") or ""),
    )

    provider = str(data.get("search_provider", "searchapi") or "searchapi").strip().lower()
    configured_search_key = str(data.get("search_api_key", "") or "")
    if provider in {"searchapi", "searchapi.io", "search_api"}:
        data["search_api_key"] = effective_env_value("SEARCHAPI_API_KEY", env_path, configured_search_key)
    elif provider == "brave":
        data["search_api_key"] = effective_env_value("BRAVE_SEARCH_API_KEY", env_path, configured_search_key)
    else:
        data["search_api_key"] = configured_search_key

    # Image generation credentials stay out of config.yaml. Agnes and
    # ModelScope/msimg are independently configurable so Dev Console can A/B test.
    data["agnes_api_key"] = effective_env_value(
        "AGNES_API_KEY",
        env_path,
        str(data.get("agnes_api_key", "") or ""),
    )
    legacy_msimg = str(data.get("msimg_api_key", "") or "")
    modelscope_fallback = effective_env_value("MODELSCOPE_API_TOKEN", env_path, legacy_msimg)
    data["msimg_api_key"] = effective_env_value("MSIMG_API_KEY", env_path, modelscope_fallback)
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


ARCHIVE_FILENAME = "archived.yaml"


def read_archive_state(persona_path: str | Path) -> str | None:
    """Return when a character was archived, or ``None`` when it is active.

    The marker sits *beside* ``persona.yaml`` rather than inside it because the
    whole persona document is handed to the model verbatim: UI lifecycle state
    must not reach the prompt. Existence decides, and the timestamp is only
    decoration -- a marker whose contents were hand-edited into nonsense still
    hides the character, since the alternative is silently un-hiding one the
    operator believes is gone.
    """

    path = Path(persona_path).parent / ARCHIVE_FILENAME
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    if isinstance(data, dict):
        return str(data.get("archived_at") or "")
    return ""


def set_character_archived(settings: Settings, character_id: str, archived: bool) -> str:
    """Archive or restore a character; return the stamp (``""`` when restored).

    Atomic, following ``persona_builder.save_persona``: a torn marker would be
    read by the next discovery as a character that vanished for no stated
    reason. Restoring removes the file rather than blanking it, so "active" has
    exactly one representation on disk.
    """

    marker = Path(resolve_persona_path(settings, character_id)).parent / ARCHIVE_FILENAME
    if not archived:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        return ""

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    temp = marker.with_suffix(".yaml.tmp")
    temp.write_text(
        yaml.safe_dump({"archived_at": stamp}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    temp.replace(marker)
    return stamp


def discover_character_profiles(settings: Settings) -> list[dict[str, str]]:
    """Discover characters directly from personas/*/persona.yaml.

    Persona files stay the single character definition source. The UI/API does
    not need a second character registry or duplicated config list. Avatar state
    is an asset concern and is projected into the public profile dynamically.

    ``archived_at`` is present only on archived characters: key presence *is*
    the flag, which keeps the value a plain string like every other key here and
    lets a caller filter without a second lookup.
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
        profile = {
            "id": character_id,
            "name": str(data.get("name") or character_id),
            "identity": str(data.get("identity") or ""),
            "tagline": str(data.get("tagline") or ""),
            "avatar_url": _avatar_url(settings, character_id),
            "persona_path": str(path),
        }
        archived_at = read_archive_state(path)
        if archived_at is not None:
            profile["archived_at"] = archived_at
        profiles.append(profile)

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


def split_archived(profiles: list[dict[str, str]], archived: bool) -> list[dict[str, str]]:
    """The listing filter, shared by every route that feeds a character picker.

    Archiving is UI lifecycle: an archived character is absent from the sidebar
    and from both member pickers, but it stays fully resolvable -- an existing
    group keeps its member, and its history keeps rendering. So the split lives
    here, at the edge that answers the browser, and never inside discovery.
    """

    return [profile for profile in profiles if ("archived_at" in profile) is archived]


def resolve_persona_path(settings: Settings, character_id: str) -> str:
    for profile in discover_character_profiles(settings):
        if profile["id"] == character_id:
            return profile["persona_path"]
    known = ", ".join(profile["id"] for profile in discover_character_profiles(settings))
    raise KeyError(f"unknown character_id={character_id!r}; known={known}")
