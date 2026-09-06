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
        state_before = self.store.get_mental_state(event.character_id)
        recent = [
            e
            for e in self.store.list_events(event.character_id, limit=10, before=event.event_time)
            if e.id != event.id
        ][-8:]
        context = compile_context(self.persona, state_before, memories, event, recent)
        reaction = self.model.react(context)
        self.store.set_mental_state(event.character_id, reaction.mental_state_update, event.event_time, event.id)

        created_memory_ids: list[int] = []
        for candidate in reaction.memory_candidates:
            saved = self.store.add_memory(
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
            if saved.id is not None:
                created_memory_ids.append(saved.id)

        created_intent_ids: list[int] = []
        for intent in reaction.intent_candidates:
            intent_id = self.store.add_intent(
                event.character_id,
                intent.content,
                intent.preferred_action.value,
                event.event_time,
                event.event_time + timedelta(hours=intent.earliest_hours),
                event.event_time + timedelta(hours=intent.expires_hours),
                reaction.action.reason,
            )
            created_intent_ids.append(intent_id)

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

        model_messages = getattr(self.model, "last_request_messages", [])
        raw_model_response = getattr(self.model, "last_response_text", "")
        model_attempt = getattr(self.model, "last_attempt", 0)
        trace = {
            "source_event_id": event.id,
            "event": event.model_dump(mode="json"),
            "context": context,
            "model_messages": model_messages,
            "raw_model_response": raw_model_response,
            "model_attempt": model_attempt,
            "mental_state_before": state_before,
            "mental_state_after": reaction.mental_state_update,
            "recalled_memories": [
                memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories
            ],
            "perception": reaction.perception,
            "reaction": reaction.reaction,
            "action": reaction.action.model_dump(mode="json"),
            "memory_candidates": [
                candidate.model_dump(mode="json") for candidate in reaction.memory_candidates
            ],
            "created_memory_ids": created_memory_ids,
            "intent_candidates": [
                candidate.model_dump(mode="json") for candidate in reaction.intent_candidates
            ],
            "created_intent_ids": created_intent_ids,
        }

        self.store.append_event(
            Event(
                character_id=event.character_id,
                event_type=EventType.ACTION,
                event_time=event.event_time,
                content=reaction.action.type.value,
                metadata={
                    "reason": reaction.action.reason,
                    "source_event_id": event.id,
                    "trace": trace,
                },
            )
        )
        return RuntimeResult(
            event=event,
            recalled_memories=memories,
            reaction=reaction,
            context=context,
            mental_state_before=state_before,
            created_memory_ids=created_memory_ids,
            created_intent_ids=created_intent_ids,
        )
