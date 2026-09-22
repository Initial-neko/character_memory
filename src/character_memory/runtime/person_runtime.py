from __future__ import annotations

from datetime import timedelta, timezone
import logging
import time
from typing import Callable

import numpy as np

from character_memory.application.action_materialization import materialize_expressive_action
from character_memory.domain.models import ActionDecision, ActionType, Event, EventType, Memory, RuntimeResult
from character_memory.runtime.person_context import PersonContextBuilder
from character_memory.runtime.reaction_engine import evaluate_reaction
from character_memory.runtime.sticker_retrieval import StickerRetriever
from character_memory.visual_runtime import direct_visual_available, generate_direct_visual_action


logger = logging.getLogger("character_memory.runtime")

_MEMORY_MIN_IMPORTANCE = 0.35
_MEMORY_DUPLICATE_SIMILARITY = 0.93


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
        self.context_builder = PersonContextBuilder(store, recall, persona)

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
        candidate_loader = getattr(self.store, "list_memory_candidates", None)
        if callable(candidate_loader):
            source_existing = candidate_loader(character_id, at=event_time)
        else:
            source_existing = self.store.list_memories(character_id)
        existing = [
            memory
            for memory in source_existing
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

            exact_finder = getattr(self.store, "find_active_memory_by_content", None)
            if callable(exact_finder):
                exact = exact_finder(character_id, content, at=event_time)
                if exact is not None:
                    decision["decision"] = "SKIP_DUPLICATE"
                    decision["duplicate_memory_id"] = exact.id
                    decision["similarity"] = 1.0
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

    @staticmethod
    def _sanitize_channel_actions(event: Event, reaction):
        """Keep channel-specific actions from leaking into chat persistence.

        Space reactions still go through PersonRuntime so Memory/Mental State/
        Intent are shared with the same person. Only the outward action surface
        changes: a Space event may never create a private CHARACTER_MESSAGE.
        """
        if event.event_type == EventType.SPACE_POST_SEEN:
            allowed = {ActionType.SPACE_LIKE, ActionType.SPACE_COMMENT}
        elif event.event_type == EventType.SPACE_COMMENT_RECEIVED:
            allowed = {ActionType.SPACE_COMMENT}
        elif event.event_type == EventType.WORLD_OBSERVATION:
            # External observations may update cognition/memory through the same
            # PersonRuntime, but they are never themselves a chat channel.
            allowed = set()
        else:
            return reaction, []

        kept = [action for action in reaction.actions if action.type in allowed]
        dropped = [
            {"type": action.type.value, "decision": "DROP_WRONG_CHANNEL"}
            for action in reaction.actions
            if action.type not in allowed
        ]
        normalized_action = kept[0] if kept else ActionDecision(type=ActionType.NO_REPLY)
        return reaction.model_copy(update={"actions": kept, "action": normalized_action}), dropped

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

        conversation_id = str(event.metadata.get("conversation_id") or f"{event.character_id}:default")
        evaluation = evaluate_reaction(
            self,
            event=event,
            exclude_event_id=event.id,
            last_chat_event=last_chat_event,
            session_id=conversation_id,
            image_data_urls=image_data_urls,
            # compile_context only exposes the tool on USER_MESSAGE turns; make
            # that product boundary explicit here instead of advertising a wider
            # capability that a lower layer later removes.
            allow_generate_image=direct_visual_available() and event.event_type == EventType.USER_MESSAGE,
        )
        timings.update(evaluation.timings)
        memories = evaluation.memories
        recent = evaluation.recent
        state_before = evaluation.state_before
        context = evaluation.context
        model_call = evaluation.model_call
        reaction = evaluation.reaction
        sticker_retrieval = evaluation.sticker_retrieval
        sticker_decisions = evaluation.sticker_decisions
        image_decisions = evaluation.image_decisions
        allowed_sticker_ids = evaluation.allowed_sticker_ids

        logger.info(
            "runtime.recall done count=%d ids=%s duration_ms=%.1f",
            len(memories),
            [memory.id for memory in memories],
            timings["recall_ms"],
        )
        logger.info(
            "runtime.context ready chars=%d recent_events=%d sticker_candidates=%d has_state=%s generate_image=%s duration_ms=%.1f",
            len(context),
            len(recent),
            len(allowed_sticker_ids),
            bool(state_before),
            direct_visual_available() and event.event_type == EventType.USER_MESSAGE,
            timings["context_ms"],
        )

        reaction, channel_decisions = self._sanitize_channel_actions(event, reaction)
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
                    materialized = materialize_expressive_action(
                        action,
                        action_index=index,
                        sticker_catalog=self.sticker_catalog,
                        image_catalog=self.image_catalog,
                        base_metadata={
                            "source_event_id": event.id,
                            "source_event_type": event.event_type.value,
                            "conversation_id": conversation_id,
                        },
                    )
                    if materialized is None:
                        continue
                    self.store.append_event(
                        Event(
                            character_id=event.character_id,
                            event_type=EventType.CHARACTER_MESSAGE,
                            event_time=event.event_time,
                            content=materialized.content,
                            metadata=materialized.metadata,
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
                    "channel_decisions": channel_decisions,
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
