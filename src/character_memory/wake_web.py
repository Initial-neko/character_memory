from __future__ import annotations

from datetime import datetime
import logging
import threading

from character_memory.web_lifecycle import background_services
from character_memory.application.async_conversation import direct_channel
from character_memory.application.wake_service import CharacterWakeService, WakeOutcome
from character_memory.domain.models import EventType


logger = logging.getLogger("character_memory.wake_web")
_WAKE_POLL_SECONDS = 30.0


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
    scheduler = getattr(access, "reaction_scheduler", None)
    if hub is None or scheduler is None:
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
        profile = next(
            (item for item in access.character_profiles() if item["id"] == character_id),
            None,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")
        if "archived_at" in profile:
            raise HTTPException(status_code=409, detail=f"Archived character cannot wake: {character_id}")

    def publish(outcome: WakeOutcome) -> None:
        channel = direct_channel(outcome.character_id, outcome.conversation_id)
        scheduler.publish_direct_responses(
            access.store(),
            outcome.character_id,
            outcome.conversation_id,
            outcome.source_event_id,
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

    def run_one(character_id: str, *, reason: str, at: datetime):
        # Re-check immediately before dispatch so an archive that races with a
        # scheduler snapshot cannot still spend an LLM call or publish output.
        ensure_character(character_id)
        outcome = service.wake(
            character_id,
            reason=reason,
            at=at,
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
                if "archived_at" in profile:
                    continue
                character_id = str(profile["id"])
                if not service.is_due(character_id, at):
                    continue
                try:
                    run_one(character_id, reason="PERIODIC", at=at)
                except Exception:
                    logger.exception("wake.periodic_failed character=%s", character_id)
        logger.info("wake.loop_stop")

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

    # This producer feeds the async SSE hub, so it has to go down before the hub
    # closes. Workers stop in reverse registration order, and async_web registers
    # before this module attaches.
    background_services(app).register("wake_loop", start=_start_wake_loop, stop=_stop_wake_loop)

    return app
