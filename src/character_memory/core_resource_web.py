from __future__ import annotations

import logging
from pathlib import Path
import time

from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.config import resolve_sticker_dir
from character_memory.stickers import import_sticker_bundle


logger = logging.getLogger("character_memory.api.resources")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def attach_core_resource_routes(app, access: CoreApiRouteAccess):
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse

    @app.get("/v1/stickers")
    def stickers(character_id: str | None = None):
        # character_id remains accepted for backward compatibility. Imported
        # stickers are global and identical across Direct and Group.
        if character_id:
            access.ensure_character(character_id)
        catalog = access.global_sticker_catalog()
        return {
            "scope": "global",
            "source": catalog.source,
            "stickers": catalog.public_items(),
        }

    @app.post("/v1/stickers/import")
    def import_stickers_web(
        archive: bytes = Body(..., media_type="application/zip"),
        character_id: str | None = None,
        filename: str = "stickers.zip",
        auto_tag: bool = True,
    ):
        if character_id:
            access.ensure_character(character_id)
        profiles = access.character_profiles()
        compatibility_persona = (
            profiles[0]["persona_path"]
            if profiles
            else access.settings.persona_path
        )
        pack_name = Path(filename).stem.strip()[:80] or "自定义表情包"
        started = time.perf_counter()
        try:
            result = import_sticker_bundle(
                compatibility_persona,
                archive,
                tagger=access.ai_sticker_tagger("global") if auto_tag else None,
                default_pack_name=pack_name,
                target_dir=resolve_sticker_dir(access.settings),
            )
            catalog = access.global_sticker_catalog()
            access.refresh_runtime_sticker_catalog(catalog)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "api.sticker import_failed scope=global file=%s error=%s",
                filename,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail=f"表情包导入失败：{exc}",
            ) from exc
        logger.info(
            "api.sticker imported scope=global file=%s count=%d ai_tagged=%d duration_ms=%.1f",
            filename,
            result["imported"],
            result["ai_tagged"],
            _ms(started),
        )
        return {
            **result,
            "scope": "global",
            "source": catalog.source,
            "stickers": catalog.public_items(),
        }

    @app.get("/v1/stickers/{sticker_id}/asset")
    def global_sticker_asset(sticker_id: str):
        catalog = access.global_sticker_catalog()
        path = catalog.asset_path(sticker_id)
        if path is None:
            raise HTTPException(status_code=404, detail="sticker not found")
        return FileResponse(path)

    @app.get("/v1/stickers/{character_id}/{sticker_id}/asset")
    def legacy_sticker_asset(character_id: str, sticker_id: str):
        access.ensure_character(character_id)
        catalog = access.global_sticker_catalog()
        path = catalog.asset_path(sticker_id)
        if path is None:
            raise HTTPException(status_code=404, detail="sticker not found")
        return FileResponse(path)

    @app.get("/v1/images")
    def images(character_id: str = "rin"):
        catalog = access.image_catalog_for(character_id)
        return {
            "character_id": character_id,
            "source": catalog.source,
            "images": catalog.public_items(character_id),
        }

    @app.get("/v1/images/{character_id}/{image_id}/asset")
    def character_image_asset(character_id: str, image_id: str):
        catalog = access.image_catalog_for(character_id)
        path = catalog.asset_path(image_id)
        if path is None:
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(path)

    @app.get("/v1/media/{media_id}")
    def uploaded_media_asset(media_id: str):
        asset = access.read_store.get_media_asset(media_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="media not found")
        path = access.media_storage.asset_path(asset)
        if path is None:
            raise HTTPException(status_code=404, detail="media file not found")
        if str(asset.mime_type).startswith(("audio/", "image/")):
            return FileResponse(
                path,
                media_type=asset.mime_type,
                headers={
                    "Content-Disposition": f'inline; filename="{asset.storage_name}"'
                },
            )
        return FileResponse(
            path,
            media_type=asset.mime_type,
            filename=asset.original_name,
        )

    return app
