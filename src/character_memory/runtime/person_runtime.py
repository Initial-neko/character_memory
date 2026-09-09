from __future__ import annotations

from datetime import timedelta, timezone
import logging
import time

import numpy as np

from character_memory.domain.models import ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.context import compile_context


logger = logging.getLogger("character_memory.runtime")

_MEMORY_MIN_IMPORTANCE = 0.35
_MEMORY_DUPLICATE_SIMILARITY = 0.93
_EXPRESSIVE_ACTIONS = {
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
}


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cosine(left: list[float], right: list[float]) -> float | None:
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    if not a.size or a.size != b.size:
        return None
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


class PersonRuntime:
    def __init__(self, store, recall, embeddings, model, persona: str):
        self.store = store
        self.recall = recall
        self.embeddings = embeddings
        self.model = model
        self.persona = persona

    def _prepare_memory_writes(self, character_id: str, event_time, candidates):
        """Small V0 admission gate: reject low-value and near-duplicate memories."""
        existing = [
            memory
            for memory in self.store.list_memories(character_id)
            if _aware(memory.event_time) <= _aware(event_time)
        ]
        comparison = [
            (memory.id, memory.content.strip().casefold(), memory.embedding)
            for memory in existing
            if memory.embedding
        ]
        accepted = []
        decisions = []

        for candidate in candidates:
            content = (candidate.content or "").strip()
            decision = {
                "candidate": candidate.model_dump(mode="json"),
                "decision": "WRITE",
                "duplicate_memory_id": None,
                "similarity": None,
            }
            if not content or candidate.importance < _MEMORY_MIN_IMPORTANCE:
                decision["decision"] = "SKIP_LOW_VALUE"
                decisions.append(decision)
                continue

            embedding = self.embeddings.embed(content)
            normalized = content.casefold()
            duplicate_id = None
            duplicate_similarity = None
            for memory_id, other_text, other_embedding in comparison:
                if normalized == other_text:
                    duplicate_id = memory_id
                    duplicate_similarity = 1.0
                    break
                similarity = _cosine(embedding, other_embedding)
                if similarity is not None and similarity >= _MEMORY_DUPLICATE_SIMILARITY:
                    duplicate_id = memory_id
                    duplicate_similarity = round(similarity, 4)
                    break

            if duplicate_similarity is not None:
                decision["decision"] = "SKIP_DUPLICATE"
                decision["duplicate_memory_id"] = duplicate_id
                decision["similarity"] = duplicate_similarity
                decisions.append(decision)
                continue

            accepted.append((candidate, embedding))
            comparison.append((None, normalized, embedding))
            decisions.append(decision)

        return accepted, decisions

    def handle(self, event: Event):
        started = time.perf_counter()
        timings: dict[str, float] = {}
        logger.info("runtime.handle start character=%s event_type=%s event_time=%s content_chars=%d", event.character_id, event.event_type.value, event.event_time.isoformat(), len(event.content or ""))

        # Capture the relationship gap before appending the new user event.
        previous_chat = self.store.list_chat_events(event.character_id, limit=1)
        last_chat_event = previous_chat[-1] if previous_chat else None

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
        context = compile_context(self.persona, state_before, memories, event, recent, last_chat_event=last_chat_event)
        timings["context_ms"] = _ms(stage)
        logger.info("runtime.context ready chars=%d recent_events=%d has_state=%s duration_ms=%.1f", len(context), len(recent), bool(state_before), timings["context_ms"])

        conversation_id = str(event.metadata.get("conversation_id") or f"{event.character_id}:default")
        stage = time.perf_counter()
        logger.info("runtime.model react start event_id=%s conversation=%s", event.id, conversation_id)
        reaction = self.model.react_for_session(context, conversation_id)
        timings["model_ms"] = _ms(stage)
        action_types = [action.type.value for action in reaction.actions]
        logger.info("runtime.model react done event_id=%s actions=%s duration_ms=%.1f memory_candidates=%d intent_candidates=%d", event.id, action_types or ["NO_REPLY"], timings["model_ms"], len(reaction.memory_candidates), len(reaction.intent_candidates))

        # Empty means "no mental-state change this turn", not "erase state".
        state_after = (reaction.mental_state_update or "").strip() or state_before

        stage = time.perf_counter()
        candidate_embeddings, memory_decisions = self._prepare_memory_writes(event.character_id, event.event_time, reaction.memory_candidates)
        timings["memory_embedding_ms"] = _ms(stage)
        created_memory_ids: list[int] = []
        created_intent_ids: list[int] = []

        stage = time.perf_counter()
        try:
            with self.store.transaction():
                if state_after:
                    self.store.set_mental_state(event.character_id, state_after, event.event_time, event.id)

                for candidate, embedding in candidate_embeddings:
                    saved = self.store.add_memory(Memory(character_id=event.character_id, content=candidate.content.strip(), memory_type=candidate.memory_type, event_time=event.event_time, importance=candidate.importance, source_event_id=event.id, embedding=embedding))
                    if saved.id is not None:
                        created_memory_ids.append(saved.id)

                reason = reaction.action.reason if reaction.action is not None else ""
                for intent in reaction.intent_candidates:
                    intent_id = self.store.add_intent(event.character_id, intent.content, intent.preferred_action.value, event.event_time, event.event_time + timedelta(hours=intent.earliest_hours), event.event_time + timedelta(hours=intent.expires_hours), reason)
                    created_intent_ids.append(intent_id)

                for index, action in enumerate(reaction.actions):
                    if action.type not in _EXPRESSIVE_ACTIONS or not (action.message or "").strip():
                        continue
                    self.store.append_event(
                        Event(
                            character_id=event.character_id,
                            event_type=EventType.CHARACTER_MESSAGE,
                            event_time=event.event_time,
                            content=(action.message or "").strip(),
                            metadata={
                                "action": action.type.value,
                                "action_index": index,
                                "source_event_id": event.id,
                                "conversation_id": conversation_id,
                            },
                        )
                    )

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
                    "last_chat_event": last_chat_event.model_dump(mode="json") if last_chat_event is not None else None,
                    "mental_state_before": state_before,
                    "mental_state_after": state_after,
                    "mental_state_updated": bool((reaction.mental_state_update or "").strip()),
                    "recalled_memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories],
                    "perception": reaction.perception,
                    "reaction": reaction.reaction,
                    "action": reaction.action.model_dump(mode="json") if reaction.action is not None else None,
                    "actions": [action.model_dump(mode="json") for action in reaction.actions],
                    "memory_candidates": [candidate.model_dump(mode="json") for candidate in reaction.memory_candidates],
                    "memory_decisions": memory_decisions,
                    "created_memory_ids": created_memory_ids,
                    "intent_candidates": [candidate.model_dump(mode="json") for candidate in reaction.intent_candidates],
                    "created_intent_ids": created_intent_ids,
                    "timings": timings,
                }
                trace_id = self.store.add_runtime_trace(event.character_id, int(event.id), event.event_time, trace)
                action_summary = ",".join(action.type.value for action in reaction.actions) or ActionType.NO_REPLY.value
                self.store.append_event(
                    Event(
                        character_id=event.character_id,
                        event_type=EventType.ACTION,
                        event_time=event.event_time,
                        content=action_summary,
                        metadata={
                            "reason": reason,
                            "actions": [action.model_dump(mode="json") for action in reaction.actions],
                            "source_event_id": event.id,
                            "conversation_id": conversation_id,
                            "trace_id": trace_id,
                        },
                    )
                )
        except Exception:
            logger.exception("runtime.derived_transaction failed event_id=%s character=%s", event.id, event.character_id)
            raise

        timings["persist_ms"] = _ms(stage)
        timings["runtime_total_ms"] = _ms(started)
        logger.info("runtime.timings event_id=%s %s", event.id, " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()))
        logger.info("runtime.handle done event_id=%s actions=%s total_ms=%.1f", event.id, action_types or ["NO_REPLY"], timings["runtime_total_ms"])
        return RuntimeResult(event=event, recalled_memories=memories, reaction=reaction, context=context, mental_state_before=state_before, created_memory_ids=created_memory_ids, created_intent_ids=created_intent_ids, timings=timings)
