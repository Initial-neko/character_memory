from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import threading
from typing import Callable

from character_memory.domain.models import EventType


logger = logging.getLogger("character_memory.application.wake")


@dataclass(frozen=True)
class WakeOutcome:
    character_id: str
    reason: str
    conversation_id: str
    source_event_id: int
    silent: bool
    result: object


class CharacterWakeService:
    """Small in-process wake coordinator.

    Wake is intentionally not a durable job system. P0.17 only gives each direct
    character an occasional TIME_TICK opportunity to think, plus a manual trigger.
    The durable facts produced by Runtime still live in the normal Event Log.
    """

    def __init__(
        self,
        store,
        get_bundle: Callable,
        character_profiles: Callable[[], list[dict]],
        *,
        interval_minutes: float = 60.0,
    ):
        self.store = store
        self.get_bundle = get_bundle
        self.character_profiles = character_profiles
        self.interval = timedelta(minutes=max(1.0, float(interval_minutes)))
        self._lock = threading.RLock()
        self._last_wake_at: dict[str, datetime] = {}

    def prime(self, now: datetime) -> None:
        """Start the periodic clock from process startup, not from the Unix epoch."""
        with self._lock:
            for profile in self.character_profiles():
                self._last_wake_at.setdefault(str(profile["id"]), now)

    def mark(self, character_id: str, now: datetime) -> None:
        with self._lock:
            self._last_wake_at[character_id] = now

    def is_due(self, character_id: str, now: datetime) -> bool:
        with self._lock:
            previous = self._last_wake_at.get(character_id)
        return previous is None or now - previous >= self.interval

    def _awaiting_proactive_reply(self, character_id: str) -> bool:
        rows = self.store.list_chat_events(character_id, limit=1)
        if not rows:
            return False
        latest = rows[-1]
        return (
            latest.event_type == EventType.CHARACTER_MESSAGE
            and latest.metadata.get("source_event_type")
            in {EventType.PROACTIVE_INTENT.value, EventType.TIME_TICK.value}
        )

    def wake(
        self,
        character_id: str,
        *,
        reason: str,
        at: datetime,
        conversation_id: str | None = None,
        force: bool = False,
    ) -> WakeOutcome | None:
        normalized_reason = str(reason or "PERIODIC").strip().upper()
        if normalized_reason not in {"PERIODIC", "MANUAL"}:
            raise ValueError(f"unknown wake reason: {reason}")

        if not force:
            if not self.is_due(character_id, at):
                return None
            if self._awaiting_proactive_reply(character_id):
                # Treat the check as a completed wake interval. Otherwise a
                # character waiting on the user would be retried every poll.
                self.mark(character_id, at)
                logger.info("wake.skip_waiting character=%s", character_id)
                return None

        # Mark before the model call. Provider failures must not turn a 60-minute
        # wake into a retry storm every 30 seconds.
        self.mark(character_id, at)
        bundle = self.get_bundle()
        result = bundle.chat.dispatch_wake(
            character_id=character_id,
            at=at,
            reason=normalized_reason,
            conversation_id=conversation_id,
        )
        resolved_conversation = str(result.event.metadata.get("conversation_id") or conversation_id or "")
        outcome = WakeOutcome(
            character_id=character_id,
            reason=normalized_reason,
            conversation_id=resolved_conversation,
            source_event_id=int(result.event.id),
            silent=not bool(result.reaction.actions),
            result=result,
        )
        logger.info(
            "wake.done character=%s reason=%s event_id=%s silent=%s conversation=%s",
            character_id,
            normalized_reason,
            outcome.source_event_id,
            outcome.silent,
            resolved_conversation,
        )
        return outcome
