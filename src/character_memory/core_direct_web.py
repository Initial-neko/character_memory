from __future__ import annotations

from datetime import datetime
import logging
import time

from character_memory.api_contracts import ChatRequest, SimulateRequest
from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.config import (
    load_persona,
    resolve_media_dir,
    resolve_persona_path,
    resolve_sticker_dir,
)


logger = logging.getLogger("character_memory.api.direct")
def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def attach_core_direct_routes(app, access: CoreApiRouteAccess):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get("/")
    def web_index():
        return FileResponse(access.web_dir / "index.html")

    @app.get("/health")
    def health():
        status = access.runtime_status()
        return {
            "ok": True,
            "web": "ready",
            "runtime_loaded": status["runtime_loaded"],
            "runtime_loading": status["runtime_loading"],
            "runtime_error": status["runtime_error"],
            "model": access.settings.chat_model,
            "vision_model": getattr(access.settings, "vision_model", ""),
            "embedding_provider": access.settings.embedding_provider,
            "db_path": access.settings.db_path,
            "media_dir": str(resolve_media_dir(access.settings)),
            "sticker_dir": str(resolve_sticker_dir(access.settings)),
            "characters": len(access.character_profiles()),
            "proactive_poll_seconds": access.proactive_poll_seconds,
        }

    @app.get("/v1/chat/history")
    def history(character_id: str = "rin", limit: int = 160):
        return access.history_payload(character_id, max(1, min(limit, 500)))

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        access.ensure_character(req.character_id)
        selected_sticker = None
        if req.sticker_id:
            catalog = access.sticker_catalog_for(req.character_id)
            sticker = catalog.get(req.sticker_id)
            if sticker is None or catalog.asset_path(req.sticker_id) is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown sticker: {req.sticker_id}",
                )
            selected_sticker = sticker.model_dump(mode="json")

        api_started = time.perf_counter()
        was_unloaded = access.current_bundle() is None
        current = access.require_bundle()
        init_ms = (
            current.init_timings.get("total_ms", 0.0)
            if was_unloaded
            else 0.0
        )

        selected_image = None
        vision_data_url = None
        if req.image is not None:
            upload_time = req.at or datetime.now().astimezone()
            try:
                asset, vision_data_url = access.media_storage.save_data_url(
                    character_id=req.character_id,
                    original_name=req.image.filename,
                    data_url=req.image.data_url,
                    created_at=upload_time,
                )
                current.store.add_media_asset(asset)
                selected_image = asset.model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception(
                    "api.media save_failed character=%s error=%s",
                    req.character_id,
                    exc,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"图片保存失败：{exc}",
                ) from exc

        logger.info(
            "api.chat start character=%s conversation=%s chars=%d sticker=%s image=%s runtime_loaded=%s",
            req.character_id,
            req.conversation_id,
            len(req.message),
            req.sticker_id or "-",
            (selected_image or {}).get("id") or "-",
            not was_unloaded,
        )

        service_started = time.perf_counter()
        try:
            out = current.chat.send(
                req.message,
                character_id=req.character_id,
                conversation_id=req.conversation_id,
                at=req.at,
                sticker=selected_sticker,
                image=selected_image,
                vision_image_data_url=vision_data_url,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception:
            logger.exception(
                "api.chat failed character=%s conversation=%s elapsed_ms=%.1f",
                req.character_id,
                req.conversation_id,
                _ms(api_started),
            )
            raise

        timings = dict(out.timings)
        timings["runtime_init_ms"] = round(init_ms, 1)
        timings["chat_service_ms"] = _ms(service_started)
        timings["api_total_ms"] = _ms(api_started)
        logger.info(
            "api.chat timings event_id=%s character=%s %s",
            out.event.id,
            req.character_id,
            " ".join(
                f"{key}={value:.1f}ms"
                for key, value in timings.items()
            ),
        )

        return {
            "event_id": out.event.id,
            "event_time": out.event.event_time.isoformat(),
            "character_id": req.character_id,
            "input_image": access.uploaded_media_payload(
                (selected_image or {}).get("id")
            ),
            "action": (
                access.action_payload(req.character_id, out.reaction.action)
                if out.reaction.action is not None
                else None
            ),
            "actions": [
                access.action_payload(req.character_id, action)
                for action in out.reaction.actions
            ],
            "perception": out.reaction.perception,
            "reaction": out.reaction.reaction,
            "mental_state": out.reaction.mental_state_update,
            "recalled_memories": [
                memory.model_dump(mode="json", exclude={"embedding"})
                for memory in out.recalled_memories
            ],
            "created_memory_ids": out.created_memory_ids,
            "created_intent_ids": out.created_intent_ids,
            "timings": timings,
        }

    @app.get("/v1/traces/{source_event_id}")
    def trace(source_event_id: int):
        value = access.read_store.get_runtime_trace(source_event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return value

    @app.get("/v1/runtime/{character_id}")
    def runtime_state(character_id: str):
        profile = access.ensure_character(character_id)
        memories = access.read_store.list_memories(
            character_id,
            include_inactive=True,
            limit=80,
            include_embedding=False,
        )
        intents = [
            dict(row)
            for row in access.read_store.list_intents(character_id, limit=80)
        ]
        stickers = access.sticker_catalog_for(character_id)
        images = access.image_catalog_for(character_id)
        current = access.current_bundle()
        if (
            current is not None
            and hasattr(current, "runtimes")
            and character_id in current.runtimes
        ):
            persona = current.runtimes[character_id].persona
        else:
            persona_path = (
                profile.get("persona_path")
                or resolve_persona_path(access.settings, character_id)
            )
            persona = load_persona(persona_path)

        status = access.runtime_status()
        return {
            "character_id": character_id,
            "profile": access.public_profile(profile),
            "now": datetime.now().astimezone().isoformat(),
            "provider": {
                "chat_model": access.settings.chat_model,
                "vision_model": getattr(access.settings, "vision_model", ""),
                "base_url": access.settings.base_url,
                "embedding_provider": access.settings.embedding_provider,
                "embedding_model": access.settings.embedding_model,
                "db_path": access.settings.db_path,
            },
            "runtime_loaded": status["runtime_loaded"],
            "runtime_error": status["runtime_error"],
            "runtime_init_timings": (
                current.init_timings
                if current is not None
                else {}
            ),
            "persona": persona,
            "mental_state": access.read_store.get_mental_state(character_id),
            "memories": [
                memory.model_dump(mode="json", exclude={"embedding"})
                for memory in reversed(memories)
            ],
            "intents": intents,
            "stickers": stickers.public_items(),
            "sticker_source": stickers.source,
            "images": images.public_items(character_id),
            "image_source": images.source,
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        current = access.require_bundle()
        return {"days": current.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        access.ensure_character(character_id)
        return {
            "world_time": access.read_store.get_world_time(character_id),
            "mental_state": access.read_store.get_mental_state(character_id),
            "recent_events": [
                event.model_dump(mode="json")
                for event in access.read_store.list_events(character_id, 30)
            ],
            "memories": [
                memory.model_dump(mode="json", exclude={"embedding"})
                for memory in access.read_store.list_memories(
                    character_id,
                    limit=30,
                    include_embedding=False,
                )
            ],
            "intents": [
                dict(row)
                for row in access.read_store.list_intents(character_id)
            ],
        }

    return app
