from __future__ import annotations

from datetime import timedelta, timezone
import logging
import time
from typing import Callable

import numpy as np

from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.context import compile_context
from character_memory.runtime.sticker_retrieval import StickerRetriever
from character_memory.visual_runtime import direct_visual_available, generate_direct_visual_action


logger = logging.getLogger("character_memory.runtime")

_MEMORY_MIN_IMPORTANCE = 0.35
_MEMORY_DUPLICATE_SIMILARITY = 0.93
_EXPRESSIVE_ACTIONS = {
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.EMOJI,
    ActionType.STICKER,
    ActionType.IMAGE,
}


class SupersededReaction(RuntimeError):
    """Raised when newer user facts arrived before derived state could commit."""


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
    def __init__(self, store, recall, embeddings, model, persona: str, sticker_catalog=None, image_catalog=None):
        self.store = store
        self.recall = recall
        self.embeddings = embeddings
        self.model = model
        self.persona = persona
        self.sticker_catalog = sticker_catalog
        self.image_catalog = image_catalog
        self.sticker_retriever = StickerRetriever(embeddings)

    def _last_chat_before(self, character_id: str, event_time, *, exclude_event_id: int | None = None):
        candidates = []
        for event_type in (EventType.USER_MESSAGE, EventType.CHARACTER_MESSAGE):
            rows = self.store.list_events(character_id, limit=4, event_type=event_type.value, before=event_time)
            rows = [item for item in rows if exclude_event_id is None or item.id != exclude_event_id]
            if rows:
                candidates.append(rows[-1])
        if not candidates:
            return None
        return max(candidates, key=lambda item: (_aware(item.event_time), item.id or 0))

    @staticmethod
    def _sticker_query(event: Event, recent: list[Event]) -> str:
        parts = [item.content.strip() for item in recent[-3:] if (item.content or "").strip()]
        current = (event.content or "").strip()
        if current:
            parts.append(current)
        return "\n".join(parts)

    def _prepare_memory_writes(self, character_id: str, event_time, candidates):
        """Small V0 admission gate: reject low-value and near-duplicate memories."""
        if not candidates:
            return [], []
        existing = [
            memory
            for memory in self.store.list_memories(character_id)
            if _aware(memory.event_time) <= _aware(event_time)
        ]
        comparison = [
            (memory.id, memory.content.strip().casefold(), memory.embedding)
            for memory in existing
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

            try:
                embedding = self.embeddings.embed(content)
            except Exception as exc:
                # Memory is optional derived cognition. A transient embedding
                # failure must not discard an otherwise valid outward reaction.
                decision["decision"] = "SKIP_EMBEDDING_ERROR"
                decision["error"] = str(exc)
                decisions.append(decision)
                logger.warning(
                    "runtime.memory embedding_failed character=%s content_chars=%d error=%s",
                    character_id,
                    len(content),
                    exc,
                )
                continue

            normalized = content.casefold()
            duplicate_id = None
            duplicate_similarity = None
            for memory_id, other_text, other_embedding in comparison:
                if normalized == other_text:
                    duplicate_id = memory_id
                    duplicate_similarity = 1.0
                    break
                if other_embedding:
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

    def _sanitize_resource_actions(self, reaction, *, allowed_sticker_ids: set[str] | None = None):
        sticker_decisions = []
        image_decisions = []
        sanitized = []
        generated_seen = False
        for action in reaction.actions:
            if action.type == ActionType.STICKER:
                if allowed_sticker_ids is not None and action.sticker_id not in allowed_sticker_ids:
                    sticker_decisions.append({"sticker_id": action.sticker_id, "decision": "DROP_NOT_RETRIEVED_STICKER"})
                    logger.warning("runtime.sticker drop_not_retrieved sticker_id=%s", action.sticker_id)
                    continue
                sticker = self.sticker_catalog.get(action.sticker_id) if self.sticker_catalog is not None else None
                if sticker is None or self.sticker_catalog.asset_path(sticker.id) is None:
                    sticker_decisions.append({"sticker_id": action.sticker_id, "decision": "DROP_UNKNOWN_STICKER"})
                    logger.warning("runtime.sticker drop_unknown sticker_id=%s", action.sticker_id)
                    continue
                sanitized.append(action)
                sticker_decisions.append({"sticker_id": action.sticker_id, "decision": "ALLOW", "label": sticker.label})
                continue

            if action.type == ActionType.IMAGE:
                image = self.image_catalog.get(action.image_id) if self.image_catalog is not None else None
                if image is None or self.image_catalog.asset_path(image.id) is None:
                    image_decisions.append({"image_id": action.image_id, "decision": "DROP_UNKNOWN_IMAGE"})
                    logger.warning("runtime.image drop_unknown image_id=%s", action.image_id)
                    continue
                sanitized.append(action)
                image_decisions.append({"image_id": action.image_id, "decision": "ALLOW", "label": image.label})
                continue

            if action.type == ActionType.GENERATE_IMAGE:
                if not direct_visual_available():
                    image_decisions.append({"decision": "DROP_GENERATION_UNAVAILABLE", "purpose": action.image_purpose})
                    continue
                if generated_seen:
                    image_decisions.append({"decision": "DROP_EXTRA_GENERATION", "purpose": action.image_purpose})
                    continue
                generated_seen = True
                sanitized.append(action)
                image_decisions.append({"decision": "ALLOW_GENERATION", "purpose": action.image_purpose})
                continue

            sanitized.append(action)

        normalized_action = sanitized[0] if sanitized else ActionDecision(type=ActionType.NO_REPLY)
        return reaction.model_copy(update={"actions": sanitized, "action": normalized_action}), sticker_decisions, image_decisions

    def handle(
        self,
        event: Event,
        *,
        image_data_urls: list[str] | None = None,
        persist_event: bool = True,
        commit_guard: Callable[[], bool] | None = None,
    ):
        started = time.perf_counter()
        timings: dict[str, float] = {}
        logger.info(
            "runtime.handle start character=%s event_type=%s event_time=%s content_chars=%d images=%d persist_event=%s",
            event.character_id,
            event.event_type.value,
            event.event_time.isoformat(),
            len(event.content or ""),
            len(image_data_urls or []),
            persist_event,
        )

        stage = time.perf_counter()
        if persist_event:
            event = self.store.append_event(event)
        elif event.id is None:
            raise ValueError("persist_event=False requires an already persisted event id")
        timings["event_store_ms"] = _ms(stage)
        logger.info("runtime.event ready id=%s duration_ms=%.1f", event.id, timings["event_store_ms"])

        last_chat_event = self._last_chat_before(
            event.character_id,
            event.event_time,
            exclude_event_id=event.id,
        )

        stage = time.perf_counter()
        memories = self.recall.recall(event.character_id, event.content, now=event.event_time)
        timings["recall_ms"] = _ms(stage)
        logger.info("runtime.recall done count=%d ids=%s duration_ms=%.1f", len(memories), [memory.id for memory in memories], timings["recall_ms"])

        stage = time.perf_counter()
        state_before = self.store.get_mental_state(event.character_id, at=event.event_time)
        recent = [e for e in self.store.list_events(event.character_id, limit=10, before=event.event_time) if e.id != event.id][-8:]
        sticker_retrieval = self.sticker_retriever.retrieve(
            self.sticker_catalog,
            self._sticker_query(event, recent),
        )
        prompt_stickers = sticker_retrieval.catalog if sticker_retrieval is not None else None
        allowed_sticker_ids = {match.sticker_id for match in sticker_retrieval.matches} if sticker_retrieval is not None else set()
        timings["sticker_retrieval_ms"] = _ms(stage)
        allow_generate_image = direct_visual_available() and event.event_type in {
            EventType.USER_MESSAGE,
            EventType.TIME_TICK,
            EventType.PROACTIVE_INTENT,
        }
        context = compile_context(
            self.persona,
            state_before,
            memories,
            event,
            recent,
            last_chat_event=last_chat_event,
            sticker_catalog=prompt_stickers,
            image_catalog=self.image_catalog,
            allow_generate_image=allow_generate_image,
        )
        timings["context_ms"] = _ms(stage)
        logger.info(
            "runtime.context ready chars=%d recent_events=%d sticker_candidates=%d has_state=%s generate_image=%s duration_ms=%.1f",
            len(context),
            len(recent),
            len(allowed_sticker_ids),
            bool(state_before),
            allow_generate_image,
            timings["context_ms"],
        )

        conversation_id = str(event.metadata.get("conversation_id") or f"{event.character_id}:default")
        stage = time.perf_counter()
        logger.info("runtime.model react start event_id=%s conversation=%s images=%d", event.id, conversation_id, len(image_data_urls or []))
        if image_data_urls:
            model_call = self.model.react_call_with_images_for_session(context, image_data_urls, conversation_id)
        else:
            model_call = self.model.react_call_for_session(context, conversation_id)
        reaction = model_call.value
        reaction, sticker_decisions, image_decisions = self._sanitize_resource_actions(
            reaction,
            allowed_sticker_ids=allowed_sticker_ids,
        )
        timings["model_ms"] = _ms(stage)
        action_types = [action.type.value for action in reaction.actions]
        model_used = model_call.trace.model or str(getattr(self.model, "model", "") or "")
        logger.info(
            "runtime.model react done event_id=%s model=%s actions=%s duration_ms=%.1f memory_candidates=%d intent_candidates=%d",
            event.id,
            model_used or "-",
            action_types or ["NO_REPLY"],
            timings["model_ms"],
            len(reaction.memory_candidates),
            len(reaction.intent_candidates),
        )

        state_after = (reaction.mental_state_update or "").strip() or state_before

        stage = time.perf_counter()
        candidate_embeddings, memory_decisions = self._prepare_memory_writes(event.character_id, event.event_time, reaction.memory_candidates)
        timings["memory_embedding_ms"] = _ms(stage)
        created_memory_ids: list[int] = []
        created_intent_ids: list[int] = []

        stage = time.perf_counter()
        try:
            with self.store.transaction():
                # The guard is deliberately evaluated *inside* the SQLiteStore
                # transaction lock. New user events cannot slip between this check
                # and the derived-state commit. A stale model result therefore
                # cannot mutate Mental State, Memory, Intent, visible Actions or Trace.
                if commit_guard is not None and not commit_guard():
                    raise SupersededReaction(f"reaction for source event {event.id} was superseded")

                if state_after:
                    self.store.set_mental_state(event.character_id, state_after, event.event_time, event.id)

                for candidate, embedding in candidate_embeddings:
                    saved = self.store.add_memory(Memory(character_id=event.character_id, content=candidate.content.strip(), memory_type=candidate.memory_type, event_time=event.event_time, importance=candidate.importance, source_event_id=event.id, embedding=embedding))
                    if saved.id is not None:
                        created_memory_ids.append(saved.id)

                reason = reaction.action.reason if reaction.action is not None else ""
                for intent in reaction.intent_candidates:
                    intent_id = self.store.add_intent(
                        event.character_id,
                        intent.content,
                        intent.preferred_action.value,
                        event.event_time,
                        event.event_time + timedelta(hours=intent.earliest_hours),
                        event.event_time + timedelta(hours=intent.expires_hours),
                        reason,
                        source_event_id=event.id,
                    )
                    created_intent_ids.append(intent_id)

                for index, action in enumerate(reaction.actions):
                    if action.type not in _EXPRESSIVE_ACTIONS:
                        continue
                    metadata = {
                        "action": action.type.value,
                        "action_index": index,
                        "source_event_id": event.id,
                        "source_event_type": event.event_type.value,
                        "conversation_id": conversation_id,
                    }
                    if action.type == ActionType.STICKER:
                        sticker = self.sticker_catalog.get(action.sticker_id) if self.sticker_catalog is not None else None
                        if sticker is None:
                            continue
                        content = f"[表情包：{sticker.label}]"
                        metadata.update({"sticker_id": sticker.id, "sticker_label": sticker.label})
                    elif action.type == ActionType.IMAGE:
                        image = self.image_catalog.get(action.image_id) if self.image_catalog is not None else None
                        if image is None:
                            continue
                        content = f"[图片：{image.label}]"
                        metadata.update({"image_id": image.id, "image_label": image.label})
                    else:
                        if not (action.message or "").strip():
                            continue
                        content = (action.message or "").strip()
                    self.store.append_event(
                        Event(
                            character_id=event.character_id,
                            event_type=EventType.CHARACTER_MESSAGE,
                            event_time=event.event_time,
                            content=content,
                            metadata=metadata,
                        )
                    )

                timings["persist_ms"] = _ms(stage)
                timings["runtime_total_ms"] = _ms(started)
                trace = {
                    "source_event_id": event.id,
                    "conversation_id": conversation_id,
                    "event": event.model_dump(mode="json"),
                    "context": context,
                    "model_messages": model_call.trace.request_messages,
                    "raw_model_response": model_call.trace.response_text,
                    "model_attempt": model_call.trace.attempt,
                    "model_used": model_used,
                    "vision_images": len(image_data_urls or []),
                    "last_chat_event": last_chat_event.model_dump(mode="json") if last_chat_event is not None else None,
                    "mental_state_before": state_before,
                    "mental_state_after": state_after,
                    "mental_state_updated": bool((reaction.mental_state_update or "").strip()),
                    "recalled_memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in memories],
                    "sticker_retrieval": {
                        "query": sticker_retrieval.query,
                        "matches": [match.__dict__ for match in sticker_retrieval.matches],
                    } if sticker_retrieval is not None else {"query": "", "matches": []},
                    "perception": reaction.perception,
                    "reaction": reaction.reaction,
                    "action": reaction.action.model_dump(mode="json") if reaction.action is not None else None,
                    "actions": [action.model_dump(mode="json") for action in reaction.actions],
                    "sticker_decisions": sticker_decisions,
                    "image_decisions": image_decisions,
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
        except SupersededReaction:
            timings["persist_ms"] = _ms(stage)
            timings["runtime_total_ms"] = _ms(started)
            logger.info("runtime.handle superseded event_id=%s total_ms=%.1f", event.id, timings["runtime_total_ms"])
            raise
        except Exception:
            logger.exception("runtime.derived_transaction failed event_id=%s character=%s", event.id, event.character_id)
            raise

        generation_action = next((action for action in reaction.actions if action.type == ActionType.GENERATE_IMAGE), None)
        if generation_action is not None:
            generation_started = time.perf_counter()
            try:
                generate_direct_visual_action(
                    self,
                    event,
                    generation_action,
                    still_current=commit_guard,
                )
            except Exception as exc:
                # Text/state actions have already committed. A provider outage must
                # not roll back or turn an otherwise valid character reaction into
                # an HTTP/SSE failure.
                logger.exception(
                    "runtime.visual_generation failed event_id=%s character=%s error=%s",
                    event.id,
                    event.character_id,
                    exc,
                )
            timings["image_generation_ms"] = _ms(generation_started)

        timings["persist_ms"] = timings.get("persist_ms", _ms(stage))
        timings["runtime_total_ms"] = _ms(started)
        logger.info("runtime.timings event_id=%s %s", event.id, " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()))
        logger.info("runtime.handle done event_id=%s actions=%s total_ms=%.1f", event.id, action_types or ["NO_REPLY"], timings["runtime_total_ms"])
        return RuntimeResult(event=event, recalled_memories=memories, reaction=reaction, context=context, mental_state_before=state_before, created_memory_ids=created_memory_ids, created_intent_ids=created_intent_ids, timings=timings)
