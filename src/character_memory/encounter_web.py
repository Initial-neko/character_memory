from __future__ import annotations

from datetime import datetime
import logging

from pydantic import BaseModel, Field

from character_memory.web_lifecycle import background_services
from character_memory.encounter import EncounterScheduler, EncounterService
from character_memory.encounter_store import EncounterRepository


logger = logging.getLogger("character_memory.encounter_web")


class EncounterCreateRequest(BaseModel):
    source_type: str = Field(default="AUTO", max_length=16)


class EncounterMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class EncounterCallRequest(BaseModel):
    session_token: str = Field(min_length=1, max_length=100)
    action: str = Field(default='CONNECTED', pattern='^(CONNECTED|ENDED|CHECK)$')


class EncounterCallMessageRequest(EncounterMessageRequest):
    session_token: str = Field(min_length=1, max_length=100)


def attach_encounter_routes(app):
    """Attach temporary random encounters without creating formal characters yet."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before encounter routes attach")

    repository = EncounterRepository(access.read_store)
    service = EncounterService(access, repository)
    scheduler = EncounterScheduler(access, repository)

    access.encounter_repository = repository
    access.encounter_service = service
    access.encounter_scheduler = scheduler

    def _start_encounter_scheduler():
        # Keep the worker alive when an API key exists; runtime settings can
        # enable/disable encounters without a restart.
        if getattr(access.settings, "api_key", ""):
            scheduler.start()

    def _stop_encounter_scheduler():
        scheduler.stop()

    background_services(app).register(
        "encounter_scheduler",
        start=_start_encounter_scheduler,
        stop=_stop_encounter_scheduler,
    )

    def active_count() -> int:
        return sum(1 for item in access.character_profiles() if "archived_at" not in item)

    def payload(candidate: dict) -> dict:
        limit = int(getattr(access, "max_active_characters", 20))
        soft_limit = int(getattr(access, "soft_active_characters", 10))
        return {
            **candidate,
            "portrait_url": service.portrait_url(candidate["id"]),
            "messages": repository.list_messages(candidate["id"], limit=50),
            'metrics': repository.metrics(candidate['id']),
            'calls': repository.list_calls(candidate['id'], now=datetime.now().astimezone()),
            'reply_pending': candidate['id'] in repository.candidates_waiting_for_reply(),
            "active_character_count": active_count(),
            "active_character_limit": limit,
            "active_character_soft_limit": soft_limit,
            "needs_confirmation": active_count() >= soft_limit and active_count() < limit,
            "can_accept": (
                candidate["status"] not in {"ACCEPTED", "DISMISSED", "EXPIRED", "FAILED"}
                and active_count() < limit
            ),
        }

    @app.get("/v1/encounters")
    def list_encounters(limit: int = 12, include_closed: bool = False, view: str = 'all', offset: int = 0):
        if view not in {'all','today','ongoing','kept'}:
            raise HTTPException(400,'invalid encounter view')
        cap=max(1,min(int(limit),50))
        now=datetime.now().astimezone()
        candidates = repository.list_candidates(
            limit=cap+1,
            include_closed=bool(include_closed),
            view=view, offset=max(0,offset), now=now,
        )
        character_limit = int(getattr(access, "max_active_characters", 20))
        return {
            "encounters": [payload(item) for item in candidates[:cap]],
            'has_more':len(candidates)>cap,
            'next_offset':max(0,offset)+cap,
            'ongoing':[{**item,'portrait_url':service.portrait_url(item['id'])} for item in repository.list_candidates(limit=50,include_closed=False,view='ongoing')],
            "pending_count": repository.pending_count(),
            "active_character_count": active_count(),
            "active_character_limit": character_limit,
            "active_character_soft_limit": int(getattr(access, "soft_active_characters", 10)),
            'today': datetime.now().astimezone().date().isoformat(),
            'daily_enabled': bool(getattr(access.settings,'encounter_daily_enabled',False)),
            'daily_slots': repository.daily_slots(datetime.now().astimezone()),
        }

    @app.get('/v1/encounters/{candidate_id:int}')
    def encounter_detail(candidate_id: int):
        candidate=repository.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(404,'来客不存在')
        return {'candidate':payload(candidate)}

    @app.get('/v1/encounters/{candidate_id}/messages')
    def encounter_history(candidate_id: int, before_id: int | None = None, limit: int = 50):
        if repository.get_candidate(candidate_id) is None:
            raise HTTPException(404,'来客不存在')
        cap=max(1,min(limit,100))
        with repository.store._lock:
            rows=repository.store.conn.execute('SELECT id,role,content,created_at FROM encounter_messages WHERE candidate_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?',
                (candidate_id,before_id,before_id,cap+1)).fetchall()
        messages=[dict(row) for row in rows[:cap]][::-1]
        return {'messages':messages,'has_more':len(rows)>cap,'next_before_id':messages[0]['id'] if messages else None}

    @app.post('/v1/encounters/{candidate_id}/calls')
    def request_encounter_call(candidate_id: int):
        try:
            return service.request_call(candidate_id)
        except KeyError as exc:
            raise HTTPException(404,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            logger.exception('encounter.call_decision_failed candidate=%s',candidate_id)
            raise HTTPException(502,'暂时无法获得电话回应，请稍后重试') from exc

    @app.post('/v1/encounters/{candidate_id}/calls/{call_id}/session')
    def encounter_call_session(candidate_id: int, call_id: str, req: EncounterCallRequest):
        try:
            with repository.lifecycle_lock(candidate_id):
                with repository.store._lock:
                    row=repository.store.conn.execute('SELECT candidate_id FROM encounter_calls WHERE id=?',(call_id,)).fetchone()
                if row is None or row['candidate_id']!=candidate_id:
                    raise KeyError('call not found')
                return repository.update_call(call_id,req.session_token,req.action,datetime.now().astimezone())
        except KeyError as exc:
            raise HTTPException(404,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post('/v1/encounters/{candidate_id}/calls/{call_id}/messages')
    def encounter_call_message(candidate_id: int, call_id: str, req: EncounterCallMessageRequest):
        with repository.lifecycle_lock(candidate_id):
            encounter_call_session(candidate_id,call_id,EncounterCallRequest(session_token=req.session_token,action='CHECK'))
            return chat_encounter(candidate_id,EncounterMessageRequest(message=req.message))

    @app.post('/v1/encounters/{candidate_id}/reply/retry')
    def retry_encounter_reply(candidate_id: int):
        with repository.lifecycle_lock(candidate_id):
            candidate=repository.get_candidate(candidate_id)
            if candidate is None:
                raise HTTPException(404,'来客不存在')
            messages=repository.list_messages(candidate_id,limit=1)
            if candidate['status'] not in {'NEW','SEEN','CHATTING'} or not messages or messages[-1]['role']!='USER':
                raise HTTPException(409,'没有可重试的临时回复')
            with repository.store._lock:
                repository.store.conn.execute("UPDATE encounter_candidates SET error='' WHERE id=?",(candidate_id,))
                repository.store._maybe_commit()
            if not scheduler.enqueue_reply(candidate_id):
                raise HTTPException(503,'回复队列繁忙，请稍后重试')
        return {'reply_pending':True}

    @app.get("/v1/encounters/status")
    def encounter_status():
        return {
            **scheduler.status(),
            "active_character_count": active_count(),
            "active_character_limit": int(getattr(access, "max_active_characters", 20)),
            "active_character_soft_limit": int(getattr(access, "soft_active_characters", 10)),
        }

    @app.post("/v1/encounters/dev/opportunity")
    def create_encounter(req: EncounterCreateRequest):
        try:
            result = service.create_candidate(
                now=datetime.now().astimezone(),
                source_type=req.source_type,
            )
            return {**result, "candidate": payload(result["candidate"])}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"邂逅生成失败：{exc}") from exc

    @app.post("/v1/encounters/dev/due")
    def force_encounter_due():
        return {"state": scheduler.force_due(now=datetime.now().astimezone())}

    @app.get("/v1/encounters/{candidate_id}/portrait/asset")
    def encounter_portrait_asset(candidate_id: int):
        from fastapi.responses import FileResponse
        if repository.get_candidate(candidate_id) is None:
            raise HTTPException(status_code=404, detail="来客不存在")
        path = service.portraits.asset_path(f"encounter-{candidate_id}") if service.portraits else None
        if path is None:
            raise HTTPException(status_code=404, detail="肖像尚未生成")
        return FileResponse(path)

    @app.post("/v1/encounters/{candidate_id}/portrait")
    def generate_encounter_portrait(candidate_id: int):
        try:
            url = service.ensure_portrait(candidate_id)
            return {"portrait_url": url}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="来客不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("encounter.portrait request_failed candidate=%s", candidate_id, exc_info=True)
            raise HTTPException(status_code=502, detail="肖像生成失败，可以稍后手动重试") from exc

    @app.post("/v1/encounters/{candidate_id}/seen")
    def see_encounter(candidate_id: int):
        try:
            return {"candidate": payload(service.mark_seen(candidate_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/encounters/{candidate_id}/messages")
    def chat_encounter(candidate_id: int, req: EncounterMessageRequest):
        try:
            # The message is stored now and answered by the scheduler thread, so
            # a slow or failing model reply can neither hold up the send nor turn
            # a message that was stored into an error the person would resend.
            result = service.post_message(candidate_id, req.message)
            if not scheduler.enqueue_reply(candidate_id):
                logger.warning("encounter.reply_queue_full candidate=%s", candidate_id)
            return {
                **result,
                "candidate": payload(result["candidate"]),
                "reply_pending": True,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"临时邂逅回复失败：{exc}") from exc

    @app.post("/v1/encounters/{candidate_id}/accept")
    def accept_encounter(candidate_id: int, confirm_over_soft_limit: bool = False):
        try:
            accepted = service.accept(
                candidate_id,
                confirm_over_soft_limit=confirm_over_soft_limit,
            )
            return {"candidate": payload(accepted)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            status = 409 if ("最多保留" in str(exc) or "closed" in str(exc)) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"留下角色失败：{exc}") from exc

    @app.post("/v1/encounters/{candidate_id}/dismiss")
    def dismiss_encounter(candidate_id: int):
        try:
            return {"candidate": payload(service.dismiss(candidate_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
