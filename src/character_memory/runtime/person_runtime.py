from __future__ import annotations

from datetime import timedelta
import logging
import time

from character_memory.domain.models import ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.context import compile_context


logger = logging.getLogger("character_memory.runtime")


class PersonRuntime:
    def __init__(self, store, recall, embeddings, model, persona: str):
        self.store = store
        self.recall = recall
        self.embeddings = embeddings
        self.model = model
        self.persona = persona

    def handle(self, event: Event):
        started = time.perf_counter()
        logger.info(
            "runtime.handle start character=%s event_type=%s event_time=%s content_chars=%d",
            event.character_id,
            event.event_type.value,
            event.event_time.isoformat(),
            len(event.content or ""),
        )

        event = self.store.append_event(event)
        logger.info("runtime.event stored id=%s", event.id)

        recall_started = time.perf_counter()
        memories = self.recall.recall(event.character_id, event.content, now=event.event_time)
        logger.info(
            "runtime.recall done count=%d ids=%s duration_ms=%d",
            len(memories),
            [memory.id for memory in memories],
            int((time.perf_counter() - recall_started) * 1000),
        )

        state_before = self.store.get_mental_state(event.character_id)
        recent = [
            e
            for e in self.store.list_events(event.character_id, limit=10, before=event.event_time)
            if e.id != event.id
        ][-8:]
        context = compile_context(self.persona, state_before, memories, event, recent)
        logger.info(
            "runtime.context ready chars=%d recent_events=%d has_state=%s",
            len(context),
            len(recent),
            bool(state_before),
        )

        model_started = time.perf_counter()
        logger.info("runtime.model react start event_id=%s", event.id)
        reaction = self.model.react(context)
        logger.info(
            "runtime.model react done event_id=%s action=%s duration_ms=%d memory_candidates=%d intent_candidates=%d",
            event.id,
            reaction.action.type.value,
            int((time.perf_counter() - model_started) * 1000),
            len(reaction.memory_candidates),
            len(reaction.intent_candidates),
        )

        self.store.set_mental_state(event.character_id, reaction.mental_state_update, event.event_time, event.id)
        logger.info("runtime.mental_state stored event_id=%s chars=%d", event.id, len(reaction.mental_state_update or ""))

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
        logger.info("runtime.memory_write done count=%d ids=%s", len(created_memory_ids), created_memory_ids)

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
        logger.info("runtime.intent_write done count=%d ids=%s", len(created_intent_ids), created_intent_ids)

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
            logger.info(
                "runtime.expression stored action=%s message_chars=%d",
                reaction.action.type.value,
                len(reaction.action.message or ""),
            )
        else:
            logger.info("runtime.expression skipped action=%s", reaction.action.type.value)

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
        logger.info(
            "runtime.handle done event_id=%s action=%s total_ms=%d",
            event.id,
            reaction.action.type.value,
            int((time.perf_counter() - started) * 1000),
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
