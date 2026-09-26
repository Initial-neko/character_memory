from __future__ import annotations

from datetime import datetime, timedelta
import logging

from character_memory.domain.models import ActionType, EXPRESSIVE_ACTIONS, EventType
from character_memory.time_utils import epoch_us


logger = logging.getLogger("character_memory.application.proactive")


class ProactiveService:
    """Dispatch only already-persisted due intents.

    This service deliberately does not create TIME_TICK events when nothing is
    due. Product scheduling should not turn idle time into repeated LLM calls.
    It also avoids stacking proactive messages while the previous proactive
    message is still unanswered by the user.

    A per-character cooldown backs that intent up. "Nothing is due" was the only
    brake, and it is not enough: a dispatch can create another due intent, and a
    model that answers with silence still spent a full reaction. The cooldown is
    consumed by *any* dispatch, silence included, and it is persisted rather than
    process-local so a restart cannot reset it.
    """

    def __init__(self, store, chat_service=None, *, min_dispatch_interval_minutes: float = 60.0):
        self.store = store
        self.chat = chat_service
        self.min_dispatch_interval = max(0.0, float(min_dispatch_interval_minutes))

    def _on_cooldown(self, character_id: str, now: datetime) -> bool:
        state = self.store.proactive_dispatch_state(character_id)
        if state is None:
            return False
        return epoch_us(now) < int(state["next_allowed_at_epoch"])

    def _note_dispatch(self, character_id: str, now: datetime, *, status: str, intent_id=None, error: str = "") -> None:
        next_at = now + timedelta(minutes=self.min_dispatch_interval)
        self.store.mark_proactive_dispatch(
            character_id,
            now,
            next_at,
            status=status,
            intent_id=intent_id,
            error=error,
            interval_minutes=self.min_dispatch_interval,
        )

    def _awaiting_user_reply(self, character_id: str) -> bool:
        rows = self.store.list_chat_events(character_id, limit=1)
        if not rows:
            return False
        latest = rows[-1]
        return (
            latest.event_type == EventType.CHARACTER_MESSAGE
            and latest.metadata.get("source_event_type") == EventType.PROACTIVE_INTENT.value
        )

    def has_due(self, character_ids: list[str], now: datetime) -> bool:
        """Gate that also honours the cooldown.

        api.dispatch_proactive_once loads the full runtime only after this
        returns True, so a character that is merely cooling down must not make it
        through here and pay for the embedding model and model client.
        """
        for character_id in character_ids:
            self.store.expire_intents(character_id, now)
            if self._on_cooldown(character_id, now):
                continue
            if self._awaiting_user_reply(character_id):
                continue
            if self.store.due_intents(character_id, now):
                return True
        return False

    def dispatch_due(self, character_ids: list[str], now: datetime) -> list[dict]:
        if self.chat is None:
            raise RuntimeError("chat_service is required to dispatch proactive intents")

        outcomes: list[dict] = []
        for character_id in character_ids:
            self.store.expire_intents(character_id, now)
            if self._on_cooldown(character_id, now):
                logger.info("proactive.skip_cooldown character=%s", character_id)
                continue
            if self._awaiting_user_reply(character_id):
                logger.info("proactive.skip_waiting character=%s", character_id)
                continue

            # One proactive intent per character per poll is enough. If multiple
            # intents are due, later polls can re-evaluate them after context has
            # changed instead of producing a burst of messages.
            rows = list(self.store.due_intents(character_id, now))[:1]
            if rows:
                # Consumed before the provider call, like wake_service: a
                # transient failure must not become a retry storm, and a round
                # the model answers with silence still spent a full reaction.
                self._note_dispatch(character_id, now, status="DISPATCHING", intent_id=int(rows[0]["id"]))
            for row in rows:
                intent_id = int(row["id"])
                self.store.set_intent_status(intent_id, "PROCESSING")
                try:
                    result = self.chat.dispatch_proactive_intent(
                        character_id=character_id,
                        intent_id=intent_id,
                        content=str(row["content"]),
                        at=now,
                    )
                    action_types = {action.type for action in result.reaction.actions}
                    legacy = result.reaction.action.type if result.reaction.action is not None else ActionType.NO_ACTION
                    if action_types & EXPRESSIVE_ACTIONS or legacy in EXPRESSIVE_ACTIONS:
                        status = "EXECUTED"
                    elif legacy == ActionType.DEFER:
                        status = "DEFERRED"
                    else:
                        status = "SUPPRESSED"
                    self.store.set_intent_status(intent_id, status)
                    self._note_dispatch(character_id, now, status=status, intent_id=intent_id)
                    outcomes.append(
                        {
                            "intent_id": intent_id,
                            "character_id": character_id,
                            "status": status,
                            "source_event_id": result.event.id,
                            "actions": [action.type.value for action in result.reaction.actions],
                        }
                    )
                    logger.info(
                        "proactive.dispatch intent_id=%s character=%s status=%s actions=%s",
                        intent_id,
                        character_id,
                        status,
                        [action.type.value for action in result.reaction.actions],
                    )
                except Exception as exc:
                    self.store.set_intent_status(intent_id, "ERROR")
                    self._note_dispatch(character_id, now, status="ERROR", intent_id=intent_id, error=str(exc))
                    logger.exception(
                        "proactive.dispatch failed intent_id=%s character=%s error=%s",
                        intent_id,
                        character_id,
                        exc,
                    )
                    outcomes.append(
                        {
                            "intent_id": intent_id,
                            "character_id": character_id,
                            "status": "ERROR",
                            "error": str(exc),
                        }
                    )
        return outcomes
