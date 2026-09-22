from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Callable

from character_memory.config import resolve_sticker_dir
from character_memory.images import load_image_catalog
from character_memory.message_projection import project_direct_message
from character_memory.stickers import StickerTagSuggestion, load_global_sticker_catalog


logger = logging.getLogger("character_memory.api.resources.service")

_STICKER_VISION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ApiResourceService:
    """Resource catalogs and wire projections shared by core API surfaces."""

    def __init__(
        self,
        *,
        settings: Any,
        read_store: Any,
        media_storage: Any,
        current_bundle: Callable[[], Any | None],
        require_bundle: Callable[[], Any],
        character_profiles: Callable[[], list[dict[str, Any]]],
        ensure_character: Callable[[str], dict[str, Any]],
    ):
        self.settings = settings
        self.read_store = read_store
        self.media_storage = media_storage
        self.current_bundle = current_bundle
        self.require_bundle = require_bundle
        self.character_profiles = character_profiles
        self.ensure_character = ensure_character

    def global_sticker_catalog(self):
        profiles = self.character_profiles()
        return load_global_sticker_catalog(
            resolve_sticker_dir(self.settings),
            persona_paths=[profile["persona_path"] for profile in profiles],
        )

    def sticker_catalog_for(self, character_id: str):
        self.ensure_character(character_id)
        current = self.current_bundle()
        if current is not None and hasattr(current, "runtimes"):
            runtime = current.runtimes.get(character_id)
            if (
                runtime is not None
                and getattr(runtime, "sticker_catalog", None) is not None
            ):
                return runtime.sticker_catalog
        return self.global_sticker_catalog()

    def refresh_runtime_sticker_catalog(self, catalog) -> None:
        current = self.current_bundle()
        if current is None or not hasattr(current, "runtimes"):
            return
        for runtime in current.runtimes.values():
            runtime.sticker_catalog = catalog

    def ai_sticker_tagger(self, scope: str = "global"):
        model_holder: dict[str, object] = {}

        def tagger(filename: str, payload: bytes) -> StickerTagSuggestion:
            suffix = Path(filename).suffix.lower()
            mime_type = _STICKER_VISION_MIME.get(suffix)
            if not mime_type:
                raise ValueError(
                    f"AI auto-tag does not support {suffix or 'this format'}; "
                    "add metadata for this sticker"
                )
            if "model" not in model_holder:
                current = self.require_bundle()
                if not hasattr(current, "model"):
                    raise RuntimeError(
                        "loaded runtime does not expose a vision model"
                    )
                model_holder["model"] = current.model
            model = model_holder["model"]
            data_url = (
                f"data:{mime_type};base64,"
                f"{base64.b64encode(payload).decode('ascii')}"
            )
            prompt = (
                "你正在给聊天软件里的表情包做长期可复用的语义标签。只观察图片本身，不猜人物真实身份、作品名或版权来源。"
                "返回 JSON：label 是 2~12 个汉字左右的简短名称；tags 是 3~8 个适合聊天检索/选择的中文短标签，优先情绪、动作、语气和使用场景；"
                "description 用一句中文说明这张表情在聊天里通常表达什么。不要输出文件名，不要输出 JSON 之外的文字。"
            )
            try:
                return model.structured_with_images_for_session(
                    prompt,
                    [data_url],
                    StickerTagSuggestion,
                    f"sticker-tag:{scope}:{filename}",
                )
            except Exception as exc:
                logger.exception(
                    "api.sticker auto_tag failed scope=%s file=%s error=%s",
                    scope,
                    filename,
                    exc,
                )
                raise RuntimeError(
                    f"AI 自动标注失败：{filename}: {exc}"
                ) from exc

        return tagger

    def image_catalog_for(self, character_id: str):
        profile = self.ensure_character(character_id)
        current = self.current_bundle()
        if current is not None and hasattr(current, "runtimes"):
            runtime = current.runtimes.get(character_id)
            if (
                runtime is not None
                and getattr(runtime, "image_catalog", None) is not None
            ):
                return runtime.image_catalog
        return load_image_catalog(profile["persona_path"])

    def sticker_payload(
        self,
        character_id: str,
        sticker_id: str | None,
    ) -> dict | None:
        if not sticker_id:
            return None
        catalog = self.sticker_catalog_for(character_id)
        sticker = catalog.get(sticker_id)
        if sticker is None or catalog.asset_path(sticker_id) is None:
            return None
        return {
            **sticker.model_dump(mode="json"),
            "url": f"/v1/stickers/{sticker.id}/asset",
        }

    def character_image_payload(
        self,
        character_id: str,
        image_id: str | None,
    ) -> dict | None:
        if not image_id:
            return None
        catalog = self.image_catalog_for(character_id)
        image = catalog.get(image_id)
        if image is None or catalog.asset_path(image_id) is None:
            return None
        return {
            **image.model_dump(mode="json"),
            "source": "CHARACTER_LIBRARY",
            "url": f"/v1/images/{character_id}/{image.id}/asset",
        }

    def uploaded_media_payload(self, media_id: str | None) -> dict | None:
        if not media_id:
            return None
        asset = self.read_store.get_media_asset(media_id)
        if (
            asset is None
            or self.media_storage.asset_path(asset) is None
        ):
            return None
        return {
            "id": asset.id,
            "label": asset.original_name,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "source": asset.source,
            "url": f"/v1/media/{asset.id}",
        }

    def action_payload(self, character_id: str, action) -> dict:
        item = action.model_dump(mode="json")
        sticker = self.sticker_payload(
            character_id,
            item.get("sticker_id"),
        )
        if sticker is not None:
            item["sticker"] = sticker
        image = self.character_image_payload(
            character_id,
            item.get("image_id"),
        )
        if image is not None:
            item["image"] = image
        return item

    def message_payload(self, event) -> dict:
        sticker = self.sticker_payload(
            event.character_id,
            event.metadata.get("sticker_id"),
        )
        image = self.character_image_payload(
            event.character_id,
            event.metadata.get("image_id"),
        )
        media_id = event.metadata.get("media_id")
        if media_id:
            image = self.uploaded_media_payload(media_id)
        return project_direct_message(
            event,
            sticker=sticker,
            image=image,
        )
