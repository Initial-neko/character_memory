from __future__ import annotations

import logging
from pathlib import Path
import time

from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.config import resolve_sticker_dir
from character_memory.llm.usage import LlmUsageStore
from character_memory.stickers import import_sticker_bundle, private_sticker_dir, remove_global_stickers
from character_memory.sticker_sheet import sticker_sheet_bundle


logger = logging.getLogger("character_memory.api.resources")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def attach_core_resource_routes(app, access: CoreApiRouteAccess):
    from fastapi import Body, HTTPException, Query
    from fastapi.responses import FileResponse

    @app.get("/v1/llm/usage")
    def llm_usage(
        hours: int = Query(default=24, ge=1, le=2160),
        limit: int = Query(default=80, ge=1, le=300),
    ):
        store = LlmUsageStore(access.settings.db_path)
        try:
            return store.usage(hours=hours, limit=limit)
        finally:
            store.close()

    @app.get("/v1/stickers")
    def stickers(character_id: str | None = None):
        # Group/user picker requests the global library without a character ID.
        # Direct picker and role runtimes see public + their own private assets.
        catalog = (
            access.sticker_catalog_for(character_id)
            if character_id else access.global_sticker_catalog()
        )
        return {
            "scope": "character" if character_id else "global",
            "source": catalog.source,
            "stickers": catalog.public_items(),
        }

    @app.post("/v1/stickers/import", openapi_extra={"requestBody": {"content": {
        "image/png": {"schema": {"type": "string", "format": "binary"}}
    }}})
    def import_stickers_web(
        archive: bytes = Body(..., media_type="application/zip"),
        character_id: str | None = None,
        filename: str = "stickers.zip",
        auto_tag: bool = True,
        normalize_background: bool = False,
        scope: str = "global",
    ):
        if scope not in {"global", "character"}:
            raise HTTPException(status_code=400, detail="scope must be global or character")
        if scope == "character" and not character_id:
            raise HTTPException(status_code=400, detail="character_id is required for private stickers")
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
            if archive.startswith(b"\x89PNG\r\n\x1a\n") or filename.lower().endswith(".png"):
                archive = sticker_sheet_bundle(archive, pack_name=pack_name, normalize_background=normalize_background)
            result = import_sticker_bundle(
                compatibility_persona,
                archive,
                tagger=access.ai_sticker_tagger(character_id if scope == "character" else "global") if auto_tag else None,
                default_pack_name=pack_name,
                target_dir=(
                    private_sticker_dir(resolve_sticker_dir(access.settings), character_id)
                    if scope == "character" else resolve_sticker_dir(access.settings)
                ),
                id_namespace=character_id if scope == "character" else None,
            )
            access.refresh_runtime_sticker_catalog(access.global_sticker_catalog())
            catalog = (
                access.sticker_catalog_for(character_id)
                if scope == "character" else access.global_sticker_catalog()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "api.sticker import_failed scope=%s character=%s file=%s error=%s",
                scope,
                character_id,
                filename,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail=f"表情包导入失败：{exc}",
            ) from exc
        logger.info(
            "api.sticker imported scope=%s character=%s file=%s count=%d ai_tagged=%d duration_ms=%.1f",
            scope,
            character_id,
            filename,
            result["imported"],
            result["ai_tagged"],
            _ms(started),
        )
        return {
            **result,
            "scope": scope,
            "source": catalog.source,
            "stickers": catalog.public_items(),
        }

    @app.delete("/v1/stickers")
    def remove_stickers(sticker_id: str | None = None, pack_id: str | None = None):
        try:
            result = remove_global_stickers(
                resolve_sticker_dir(access.settings), sticker_id=sticker_id, pack_id=pack_id,
                persona_paths=[profile["persona_path"] for profile in access.character_profiles()],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="sticker or pack not found") from exc
        catalog = access.global_sticker_catalog()
        access.refresh_runtime_sticker_catalog(catalog)
        return {**result, "scope": "global", "stickers": catalog.public_items()}

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
        catalog = access.sticker_catalog_for(character_id)
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
