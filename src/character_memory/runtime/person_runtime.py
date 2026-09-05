from __future__ import annotations

from datetime import timedelta

from character_memory.domain.models import ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.context import compile_context


class PersonRuntime:
    def __init__(self, store, recall, embeddings, model, persona: str):
        self.store = store
        self.recall = recall
        self.embeddings = embeddings
        self.model = model
        self.persona = persona

    def handle(self, event: Event):
        event = self.store.append_event(event)
        memories = self.recall.recall(event.character_id, event.content, now=event.event_time)
        state = self.store.get_mental_state(event.character_id)
        recent = [
            e
            for e in self.store.list_events(event.character_id, limit=10, before=event.event_time)
            if e.id != event.id
        ][-8:]
        reaction = self.model.react(compile_context(self.persona, state, memories, event, recent))
        self.store.set_mental_state(event.character_id, reaction.mental_state_update, event.event_time, event.id)

        for candidate in reaction.memory_candidates:
            self.store.add_memory(
                Memory(
                    character_id=event.character_id,
                    content=candidate.content,
                    memory_type=candidate.memory_type,
                    event_time=event.event_time,
                    importance=candidate.importance,
                    source_event_id=event.id,
                    embedding=self.embeddings.embed(candidate.content),
                )
            )

        for intent in reaction.intent_candidates:
            self.store.add_intent(
                event.character_id,
                intent.content,
                intent.preferred_action.value,
                event.event_time,
                event.event_time + timedelta(hours=intent.earliest_hours),
                event.event_time + timedelta(hours=intent.expires_hours),
                reaction.action.reason,
            )

        if reaction.action.type in {ActionType.REPLY, ActionType.MINIMAL_RESPONSE, ActionType.PROACTIVE_MESSAGE}:
            self.store.append_event(
                Event(
                    character_id=event.character_id,
                    event_type=EventType.CHARACTER_MESSAGE,
                    event_time=event.event_time,
                    content=reaction.action.message or "",
                    metadata={"action": reaction.action.type.value, "source_event_id": event.id},
                )
            )

        self.store.append_event(
            Event(
                character_id=event.character_id,
                event_type=EventType.ACTION,
                event_time=event.event_time,
                content=reaction.action.type.value,
                metadata={"reason": reaction.action.reason, "source_event_id": event.id},
            )
        )
        return RuntimeResult(event=event, recalled_memories=memories, reaction=reaction)
