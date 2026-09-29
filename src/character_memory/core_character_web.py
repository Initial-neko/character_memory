from __future__ import annotations

import logging
import time

from character_memory.api_contracts import (
    CharacterCapacityConfirmationRequired,
    CharacterCapacityExceeded,
    CreateCharacterRequest,
    DeleteArchivedCharacterRequest,
    MAX_ACTIVE_CHARACTERS,
    PersonaDraftRequest,
    SOFT_ACTIVE_CHARACTERS,
)
from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.app import build_model
from character_memory.character_activity import CharacterActivityReader
from character_memory.character_onboarding import persona_inspector_payload
from character_memory.config import split_archived, split_direct_pending
from character_memory.persona_builder import PersonaBuilder


logger = logging.getLogger("character_memory.api.character")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def attach_core_character_routes(app, access: CoreApiRouteAccess):
    from fastapi import HTTPException

    # Built once here, not per request: both repositories it holds run
    # CREATE TABLE IF NOT EXISTS in their constructor.
    activity_reader = CharacterActivityReader(access.read_store)

    @app.get("/v1/characters")
    def characters(archived: bool = False, include_deferred: bool = False):
        profiles = access.character_profiles()
        listed = split_archived(profiles, archived)
        active_total = len(split_archived(profiles, False))
        if not archived and not include_deferred:
            # Characters a batch build created are deferred: alive everywhere
            # else, but absent from the sidebar, which is how the user asked for
            # a new group to be introduced ("先进群，不进私聊栏"). Only the
            # sidebar's own request filters them -- the member pickers, Space,
            # voice and World all still have to offer them, and they ask for
            # ``include_deferred`` instead.
            #
            # The fallback is load-bearing, not politeness: the browser treats
            # an empty list as "没有发现任何 Persona" and refuses to boot, so if
            # deferring would hide every remaining character we show them
            # instead of bricking the app.
            listed = split_direct_pending(listed, False) or listed
        characters = [access.public_profile(profile) for profile in listed]
        if archived:
            # Only the archive drawer renders activity, and it only ever lists
            # archived characters, so counting the active list would spend
            # three queries per character on numbers nothing displays.
            for payload in characters:
                payload["activity"] = activity_reader.for_character(
                    str(payload.get("id", ""))
                ).payload()
        return {
            "characters": characters,
            "soft_limit": SOFT_ACTIVE_CHARACTERS,
            "active_limit": MAX_ACTIVE_CHARACTERS,
            "active_total": active_total,
            "overflow_count": 0 if archived else max(0, active_total - SOFT_ACTIVE_CHARACTERS),
        }

    @app.post("/v1/characters/{character_id}/open-direct")
    def open_direct_character(character_id: str):
        # No voice-registry refresh: a deferred character was never withdrawn
        # from the roster the GSV sidecar resolves, so opening its sidebar entry
        # changes nothing for voice. Paying the reload stall here would be pure
        # cost on a button the user may press several times in a row.
        return access.open_direct_character(character_id)

    @app.get("/v1/characters/summaries")
    def character_summaries(archived: bool = False):
        listed = split_archived(access.character_profiles(), archived)
        return {"characters": [access.character_summary(profile) for profile in listed]}

    @app.get("/v1/characters/{character_id}/persona")
    def character_persona(character_id: str):
        profile = access.ensure_character(character_id)
        try:
            return persona_inspector_payload(profile)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _set_archived_and_refresh_voice(character_id: str, *, archived: bool, confirm_over_soft_limit: bool = False):
        result = access.set_archived(
            character_id,
            archived=archived,
            confirm_over_soft_limit=confirm_over_soft_limit,
        )
        result["voice_registry"] = access.refresh_voice_registry()
        return result

    @app.post("/v1/characters/{character_id}/archive")
    def archive_character(character_id: str):
        return _set_archived_and_refresh_voice(character_id, archived=True)

    @app.post("/v1/characters/{character_id}/restore")
    def restore_character(character_id: str, confirm_over_soft_limit: bool = False):
        return _set_archived_and_refresh_voice(
            character_id,
            archived=False,
            confirm_over_soft_limit=confirm_over_soft_limit,
        )

    # POST, not DELETE, on purpose -- even though this is the delete. A DELETE
    # would have to carry `confirm_name` in its body, and a body on DELETE is
    # the one part of the method that intermediaries are allowed to drop; a
    # dropped body would look like an empty confirmation rather than an error.
    # It also reads as the third member of the family it belongs to:
    # /archive, /restore, /delete all take the character in the path and their
    # arguments in a body.
    @app.post("/v1/characters/{character_id}/delete")
    def delete_character(character_id: str, req: DeleteArchivedCharacterRequest):
        try:
            result = access.delete_archived_character(
                character_id,
                confirm_name=req.confirm_name,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "api.character delete failed character=%s error=%s",
                character_id,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"人物删除失败：{exc}") from exc
        # Same contract as archive/restore: a running GSV sidecar must stop
        # resolving the character now, and voice.yaml is gone with the persona.
        result["voice_registry"] = access.refresh_voice_registry()
        return result

    @app.post("/v1/characters/draft")
    def generate_character_draft(req: PersonaDraftRequest):
        started = time.perf_counter()
        temporary_model = None
        try:
            current = access.current_bundle()
            if current is not None and hasattr(current, "model"):
                model = current.model
            else:
                temporary_model = build_model(access.settings)
                model = temporary_model
            draft = PersonaBuilder(model).generate(
                req.description,
                name=req.name,
                age=req.age,
                tags=req.tags,
            )
            logger.info(
                "api.persona_draft done name=%s duration_ms=%.1f",
                draft.name,
                _ms(started),
            )
            return {"draft": draft.model_dump(mode="json")}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "api.persona_draft failed duration_ms=%.1f error=%s",
                _ms(started),
                exc,
            )
            raise HTTPException(status_code=502, detail=f"人物草稿生成失败：{exc}") from exc
        finally:
            if temporary_model is not None:
                temporary_model.close()

    @app.post("/v1/characters")
    def create_character(req: CreateCharacterRequest):
        try:
            profile = access.create_character_from_draft(
                req.draft,
                req.character_id,
                confirm_over_soft_limit=req.confirm_over_soft_limit,
                creation=req.creation.model_dump(mode="json") if req.creation is not None else None,
            )
        except CharacterCapacityConfirmationRequired as exc:
            raise HTTPException(status_code=409, detail=exc.detail()) from exc
        except CharacterCapacityExceeded as exc:
            raise HTTPException(status_code=409, detail=exc.detail()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "api.character create failed name=%s error=%s",
                req.draft.name,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"人物创建失败：{exc}") from exc
        return {
            "character": access.public_profile(profile),
            "description": req.draft.description,
        }

    return app
