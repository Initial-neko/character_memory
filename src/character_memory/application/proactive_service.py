from __future__ import annotations

from datetime import datetime
import logging

from character_memory.domain.models import ActionType


logger = logging.getLogger("character_memory.application.proactive")

_EXPRESSIVE = {
    ActionType.PROACTIVE_MESSAGE,
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
}


class ProactiveService:
    """Dispatch only already-persisted due intents.

    This service deliberately does not create TIME_TICK events when nothing is
    due. Product scheduling should not turn idle time into repeated LLM calls.
    """

    def __init__(self, store, chat_service):
        self.store = store
        self.chat = chat_service

    def has_due(self, character_ids: list[str], now: datetime) -> bool:
        for character_id in character_ids:
            self.store.expire_intents(character_id, now)
            if self.store.due_intents(character_id, now):
                return True
        return False

    def dispatch_due(self, character_ids: list[str], now: datetime) -> list[dict]:
        outcomes: list[dict] = []
        for character_id in character_ids:
            self.store.expire_intents(character_id, now)
            rows = list(self.store.due_intents(character_id, now))
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
                    if action_types & _EXPRESSIVE or legacy in _EXPRESSIVE:
                        status = "EXECUTED"
                    elif legacy == ActionType.DEFER:
                        status = "DEFERRED"
                    else:
                        status = "SUPPRESSED"
                    self.store.set_intent_status(intent_id, status)
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
