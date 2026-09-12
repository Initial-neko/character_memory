from __future__ import annotations

from datetime import datetime
import logging
import threading

from pydantic import BaseModel, Field

from character_memory.application.async_conversation import direct_channel
from character_memory.application.wake_service import CharacterWakeService, WakeOutcome
from character_memory.domain.models import EventType


logger = logging.getLogger("character_memory.wake_web")
_WAKE_POLL_SECONDS = 30.0


class ManualWakeRequest(BaseModel):
    conversation_id: str = Field(default="", max_length=160)


def attach_wake_routes(app):
    """Attach minimal direct-character wake behavior.

    Scheduling is intentionally process-local. No new database tables, durable
    jobs, group wake, or crash recovery are introduced in P0.17.
    """
    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before wake routes are attached")
    hub = getattr(access, "stream_hub", None)
    if hub is None:
        raise RuntimeError("attach_async_routes() must run before attach_wake_routes()")

    settings = access.settings
    service = CharacterWakeService(
        access.store(),
        access.get_bundle,
        access.character_profiles,
        interval_minutes=float(getattr(settings, "proactive_wake_minutes", 60.0)),
    )
    access.wake_service = service
    stop = threading.Event()
    thread: threading.Thread | None = None

    def ensure_character(character_id: str) -> None:
        if not any(profile["id"] == character_id for profile in access.character_profiles()):
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")

    def response_events(outcome: WakeOutcome):
        store = access.store()
        with store._lock:
            rows = store.conn.execute(
                "SELECT * FROM events WHERE character_id=? AND event_type=? "
                "AND CAST(json_extract(metadata_json,'$.source_event_id') AS INTEGER)=? ORDER BY id",
                (
                    outcome.character_id,
                    EventType.CHARACTER_MESSAGE.value,
                    int(outcome.source_event_id),
                ),
            ).fetchall()
        return [store._event_from_row(row) for row in rows]

    def publish(outcome: WakeOutcome) -> None:
        channel = direct_channel(outcome.character_id, outcome.conversation_id)
        for event in response_events(outcome):
            hub.publish(
                channel,
                "character_event",
                {
                    "id": event.id,
                    "character_id": event.character_id,
                    "event_type": event.event_type.value,
                    "event_time": event.event_time.isoformat(),
                    "content": event.content,
                    "metadata": event.metadata,
                },
            )
        hub.publish(
            channel,
            "reaction_complete",
            {
                "watermark": outcome.source_event_id,
                "silent": outcome.silent,
                "wake_reason": outcome.reason,
                "source_event_type": EventType.TIME_TICK.value,
            },
        )

    def run_one(character_id: str, *, reason: str, at: datetime, conversation_id: str | None = None, force: bool = False):
        outcome = service.wake(
            character_id,
            reason=reason,
            at=at,
            conversation_id=conversation_id,
            force=force,
        )
        if outcome is not None:
            publish(outcome)
        return outcome

    def loop() -> None:
        now = datetime.now().astimezone()
        service.prime(now)
        logger.info(
            "wake.loop_start enabled=%s interval_minutes=%.1f poll_seconds=%.0f",
            bool(getattr(settings, "proactive_wake_enabled", True)),
            float(getattr(settings, "proactive_wake_minutes", 60.0)),
            _WAKE_POLL_SECONDS,
        )
        while not stop.wait(_WAKE_POLL_SECONDS):
            if not bool(getattr(settings, "proactive_wake_enabled", True)):
                continue
            # Do not initialize a remote runtime that cannot possibly work.
            if not str(getattr(settings, "api_key", "") or "").strip():
                continue
            at = datetime.now().astimezone()
            for profile in access.character_profiles():
                if stop.is_set():
                    break
                character_id = str(profile["id"])
                if not service.is_due(character_id, at):
                    continue
                try:
                    run_one(character_id, reason="PERIODIC", at=at)
                except Exception:
                    logger.exception("wake.periodic_failed character=%s", character_id)
        logger.info("wake.loop_stop")

    @app.post("/v1/characters/{character_id}/wake")
    def manual_wake(character_id: str, req: ManualWakeRequest):
        ensure_character(character_id)
        if not str(getattr(settings, "api_key", "") or "").strip():
            raise HTTPException(status_code=503, detail="Runtime 未配置 API key，无法唤醒人物")
        at = datetime.now().astimezone()
        try:
            outcome = run_one(
                character_id,
                reason="MANUAL",
                at=at,
                conversation_id=req.conversation_id.strip() or None,
                force=True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("wake.manual_failed character=%s error=%s", character_id, exc)
            raise HTTPException(status_code=502, detail=f"唤醒失败：{exc}") from exc
        if outcome is None:
            raise HTTPException(status_code=409, detail="人物当前无法唤醒")
        return {
            "character_id": character_id,
            "reason": outcome.reason,
            "conversation_id": outcome.conversation_id,
            "source_event_id": outcome.source_event_id,
            "silent": outcome.silent,
            "actions": [action.model_dump(mode="json") for action in outcome.result.reaction.actions],
            "created_intent_ids": list(outcome.result.created_intent_ids),
        }

    @app.on_event("startup")
    def _start_wake_loop():
        nonlocal thread
        if thread is None or not thread.is_alive():
            stop.clear()
            thread = threading.Thread(target=loop, name="character-memory-wake", daemon=True)
            thread.start()

    def _stop_wake_loop():
        stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    # attach_wake_routes runs after async routes. Insert at the front so this
    # producer stops before the SSE hub/scheduler are closed by async shutdown.
    app.router.on_shutdown.insert(0, _stop_wake_loop)

    return app
