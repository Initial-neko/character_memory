from __future__ import annotations

from datetime import timedelta
import logging
import time

from character_memory.domain.models import ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.context import compile_context


logger = logging.getLogger("character_memory.runtime")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class PersonRuntime:
    def __init__(self, store, recall, embeddings, model, persona: str):
        self.store = store
        self.recall = recall
        self.embeddings = embeddings
        self.model = model
        self.persona = persona

    def handle(self, event: Event):
        started = time.perf_counter()
        timings: dict[str, float] = {}
        logger.info("runtime.handle start character=%s event_type=%s event_time=%s content_chars=%d", event.character_id, event.event_type.value, event.event_time.isoformat(), len(event.content or ""))

        stage = time.perf_counter()
        event = self.store.append_event(event)
        timings["event_store_ms"] = _ms(stage)
        logger.info("runtime.event stored id=%s duration_ms=%.1f", event.id, timings["event_store_ms"])

        stage = time.perf_counter()
        memories = self.recall.recall(event.character_id, event.content, now=event.event_time)
        timings["recall_ms"] = _ms(stage)
        logger.info("runtime.recall done count=%d ids=%s duration_ms=%.1f", len(memories), [memory.id for memory in memories], timings["recall_ms"])

        stage = time.perf_counter()
        state_before = self.store.get_mental_state(event.character_id)
        recent = [e for e in self.store.list_events(event.character_id, limit=10, before=event.event_time) if e.id != event.id][-8:]
        context = compile_context(self.persona, state_before, memories, event, recent)
        timings["context_ms"] = _ms(stage)
        logger.info("runtime.context ready chars=%d recent_events=%d has_state=%s duration_ms=%.1f", len(context), len(recent), bool(state_before), timings["context_ms"])

        conversation_id = str(event.metadata.get("conversation_id") or f"{event.character_id}:default")
        stage = time.perf_counter()
        logger.info("runtime.model react start event_id=%s conversation=%s", event.id, conversation_id)
        reaction = self.model.react_for_session(context, conversation_id)
        timings["model_ms"] = _ms(stage)
        logger.info("runtime.model react done event_id=%s action=%s duration_ms=%.1f memory_candidates=%d intent_candidates=%d", event.id, reaction.action.type.value, timings["model_ms"], len(reaction.memory_candidates), len(reaction.intent_candidates))

        # Empty means "no mental-state change this turn", not "erase state".
        state_after = (reaction.mental_state_update or "").strip() or state_before

        stage = time.perf_counter()
        candidate_embeddings = [(candidate, self.embeddings.embed(candidate.content)) for candidate in reaction.memory_candidates]
        timings["memory_embedding_ms"] = _ms(stage)
        created_memory_ids: list[int] = []
        created_intent_ids: list[int] = []

        stage = time.perf_counter()
        try:
            with self.store.transaction():
                if state_after:
                    self.store.set_mental_state(event.character_id, state_after, event.event_time, event.id)

                for candidate, embedding in candidate_embeddings:
                    saved = self.store.add_memory(Memory(character_id=event.character_id, content=candidate.content, memory_type=candidate.memory_type, event_time=event.event_time, importance=candidate.importance, source_event_id=event.id, embedding=embedding))
                    if saved.id is not None:
                        created_memory_ids.append(saved.id)

                for intent in reaction.intent_candidates:
                    intent_id = self.store.add_intent(event.character_id, intent.content, intent.preferred_action.value, event.event_time, event.event_time + timedelta(hours=intent.earliest_hours), event.event_time + timedelta(hours=intent.expires_hours), reaction.action.reason)
                    created_intent_ids.append(intent_id)

                if reaction.action.type in {ActionType.REPLY, ActionType.MINIMAL_RESPONSE, ActionType.PROACTIVE_MESSAGE}:
                    self.store.append_event(Event(character_id=event.character_id, event_type=EventType.CHARACTER_MESSAGE, event_time=event.event_time, content=reaction.action.message or "", metadata={"action": reaction.action.type.value, "source_event_id": event.id, "conversation_id": conversation_id}))

                timings["persist_ms"] = _ms(stage)
                timings["runtime_total_ms"] = _ms(started)
                model_messages = getattr(self.model, "last_request_messages", [])
                raw_model_response = getattr(self.model, "last_response_text", "")
                model_attempt = getattr(self.model, "last_attempt", 0)
                trace = {
                    "source_event_id": event.id,
                    "conversation_id": conversation_id,
                    "event": event.model_dump(mode="json"),
                    "context": context,
                    "model_messages": model_messages,
                    "raw_model_response": raw_model_response,
                    "model_attempt": model_attempt,
                    "mental_state_before": state_before,
                    "mental_state_after": state_after,
                    "mental_state_updated": bool((reaction.mental_state_update or "").strip()),
                    "recalled_memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories],
                    "perception": reaction.perception,
                    "reaction": reaction.reaction,
                    "action": reaction.action.model_dump(mode="json"),
                    "memory_candidates": [candidate.model_dump(mode="json") for candidate in reaction.memory_candidates],
                    "created_memory_ids": created_memory_ids,
                    "intent_candidates": [candidate.model_dump(mode="json") for candidate in reaction.intent_candidates],
                    "created_intent_ids": created_intent_ids,
                    "timings": timings,
                }
                trace_id = self.store.add_runtime_trace(event.character_id, int(event.id), event.event_time, trace)
                self.store.append_event(Event(character_id=event.character_id, event_type=EventType.ACTION, event_time=event.event_time, content=reaction.action.type.value, metadata={"reason": reaction.action.reason, "source_event_id": event.id, "conversation_id": conversation_id, "trace_id": trace_id}))
        except Exception:
            logger.exception("runtime.derived_transaction failed event_id=%s character=%s", event.id, event.character_id)
            raise

        timings["persist_ms"] = _ms(stage)
        timings["runtime_total_ms"] = _ms(started)
        logger.info("runtime.timings event_id=%s %s", event.id, " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()))
        logger.info("runtime.handle done event_id=%s action=%s total_ms=%.1f", event.id, reaction.action.type.value, timings["runtime_total_ms"])
        return RuntimeResult(event=event, recalled_memories=memories, reaction=reaction, context=context, mental_state_before=state_before, created_memory_ids=created_memory_ids, created_intent_ids=created_intent_ids, timings=timings)
