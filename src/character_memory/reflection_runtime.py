"""Compose the reflection scheduler and read-only projection (P08)."""
from __future__ import annotations

import logging
import threading

from character_memory.application.reflection_service import ReflectionService
from character_memory.reflection_web import attach_reflection_routes


logger = logging.getLogger("character_memory.reflection")


class ReflectionScheduler:
    """One process-local poller; the durable period ledger owns correctness."""

    def __init__(self, runner, *, poll_seconds: float, enabled: bool = True):
        self._runner = runner
        self._poll_seconds = max(10.0, min(float(poll_seconds), 3600.0))
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="reflection-scheduler", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout=10.0)
        with self._lock:
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._runner()
            except Exception:
                logger.exception("reflection.cycle_failed")
            self._stop.wait(self._poll_seconds)


def attach_reflection_runtime(app, store, background, settings, *, own_bundle: bool) -> None:
    """Attach the read-only route and one worker to the shared lifecycle."""
    access = app.state.character_memory
    attach_reflection_routes(app, access)
    state: dict[str, ReflectionService | None] = {"service": None}

    def run_once() -> None:
        if not bool(getattr(settings, "reflection_enabled", False)):
            return
        current_bundle = getattr(access, "current_bundle", None)
        if callable(current_bundle) and current_bundle() is None:
            # Paid work must wait for the real model bundle. Claiming a period
            # before the model exists would turn a harmless warmup into FAILED.
            return
        if state["service"] is None:
            state["service"] = ReflectionService(
                store,
                settings,
                model_provider=lambda: access.require_bundle().model,
            )
        profiles = [
            profile["id"] for profile in access.character_profiles()
            if profile.get("id") and not profile.get("archived_at")
        ]
        state["service"].run_all(profiles)

    scheduler = ReflectionScheduler(
        run_once,
        poll_seconds=float(getattr(settings, "reflection_poll_seconds", 300.0)),
        enabled=bool(getattr(settings, "reflection_enabled", False)) and own_bundle,
    )
    background.register("reflection", start=scheduler.start, stop=scheduler.stop)


__all__ = ["ReflectionScheduler", "attach_reflection_runtime"]
