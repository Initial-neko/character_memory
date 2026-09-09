from __future__ import annotations

from character_memory.domain.models import ActionType, Event, EventType


class TimeTicker:
    """Turns world time and pending intents into ordinary PersonRuntime events."""

    def __init__(self, store, runtime):
        self.store = store
        self.runtime = runtime

    def tick(self, character_id, now):
        self.store.expire_intents(character_id, now)
        results = []
        due = self.store.due_intents(character_id, now)

        for row in due:
            result = self.runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.PROACTIVE_INTENT,
                    event_time=now,
                    content=f"之前留下的意图：{row['content']}。现在重新判断是否执行、延后或放弃。",
                    metadata={"intent_id": row["id"]},
                )
            )
            results.append(result)
            action = result.reaction.action.type
            if action in {ActionType.PROACTIVE_MESSAGE, ActionType.REPLY, ActionType.MINIMAL_RESPONSE, ActionType.MESSAGE, ActionType.EMOJI}:
                status = "EXECUTED"
            elif action == ActionType.DEFER:
                status = "DEFERRED"
            else:
                status = "SUPPRESSED"
            self.store.set_intent_status(row["id"], status)

        if not due:
            results.append(
                self.runtime.handle(
                    Event(
                        character_id=character_id,
                        event_type=EventType.TIME_TICK,
                        event_time=now,
                        content=f"现在时间是 {now.isoformat()}。没有用户新消息；判断此刻是否有自然、真实理由主动行动。",
                    )
                )
            )
        return results
