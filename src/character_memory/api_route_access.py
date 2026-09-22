from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class CoreApiRouteAccess:
    """Dependencies consumed by the core HTTP route modules.

    create_api() owns lifecycle and mutable runtime composition. Route modules
    receive this explicit adapter instead of closing over api.py internals, so
    parallel feature work can touch character/resources/direct surfaces without
    repeatedly editing the composition root.
    """

    settings: Any
    web_dir: Path
    read_store: Any
    media_storage: Any
    current_bundle: Callable[[], Any | None]
    require_bundle: Callable[[], Any]
    runtime_status: Callable[[], dict[str, Any]]
    proactive_poll_seconds: float

    character_profiles: Callable[[], list[dict[str, Any]]]
    public_profile: Callable[[dict[str, Any]], dict[str, Any]]
    set_archived: Callable[..., dict[str, Any]]
    character_summary: Callable[[dict[str, Any]], dict[str, Any]]
    ensure_character: Callable[[str], dict[str, Any]]
    create_character_from_draft: Callable[..., dict[str, Any]]

    global_sticker_catalog: Callable[[], Any]
    sticker_catalog_for: Callable[[str], Any]
    ai_sticker_tagger: Callable[..., Any]
    refresh_runtime_sticker_catalog: Callable[[Any], None]
    image_catalog_for: Callable[[str], Any]
    uploaded_media_payload: Callable[[str | None], dict[str, Any] | None]
    action_payload: Callable[[str, Any], dict[str, Any]]

    history_payload: Callable[[str, int], dict[str, Any]]
