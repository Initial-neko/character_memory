from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from character_memory.domain.models import Event
from character_memory.runtime.context import compile_context


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


@dataclass
class ReactionEvaluation:
    """Shared model-evaluation result before a channel persists derived facts."""

    context: str
    reaction: Any
    model_call: Any
    memories: list
    recent: list[Event]
    state_before: str
    sticker_retrieval: Any
    sticker_decisions: list[dict]
    image_decisions: list[dict]
    allowed_sticker_ids: set[str]
    timings: dict[str, float]


def evaluate_reaction(
    runtime,
    *,
    event: Event,
    query: str | None = None,
    recent_events: list[Event] | None = None,
    recent_limit: int = 8,
    exclude_event_id: int | None = None,
    last_chat_event: Event | None = None,
    sticker_query: str | None = None,
    sticker_query_builder: Callable[[list[Event]], str] | None = None,
    context_suffix: str = "",
    session_id: str | None = None,
    image_data_urls: list[str] | None = None,
    allow_generate_image: bool = False,
) -> ReactionEvaluation:
    """Run the shared Recall -> Context -> Model -> resource-sanitize pipeline."""

    timings: dict[str, float] = {}

    stage = time.perf_counter()
    person_context = runtime.context_builder.build(
        event.character_id,
        query=query if query is not None else event.content,
        at=event.event_time,
        exclude_event_id=exclude_event_id,
        recent_events=recent_events,
        recent_limit=recent_limit,
    )
    memories = person_context.memories
    state_before = person_context.mental_state
    recent = person_context.recent_events
    timings["recall_ms"] = _ms(stage)

    stage = time.perf_counter()
    resolved_sticker_query = sticker_query
    if sticker_query_builder is not None:
        resolved_sticker_query = sticker_query_builder(recent)
    if resolved_sticker_query is None:
        resolved_sticker_query = runtime._sticker_query(event, recent)
    sticker_retrieval = runtime.sticker_retriever.retrieve(runtime.sticker_catalog, resolved_sticker_query)
    prompt_stickers = sticker_retrieval.catalog if sticker_retrieval is not None else None
    allowed_sticker_ids = (
        {match.sticker_id for match in sticker_retrieval.matches}
        if sticker_retrieval is not None
        else set()
    )
    timings["sticker_retrieval_ms"] = _ms(stage)

    stage = time.perf_counter()
    context = compile_context(
        runtime.persona,
        state_before,
        memories,
        event,
        recent,
        last_chat_event=last_chat_event,
        sticker_catalog=prompt_stickers,
        image_catalog=runtime.image_catalog,
        allow_generate_image=allow_generate_image,
    )
    if context_suffix:
        context += context_suffix
    timings["context_ms"] = _ms(stage)

    resolved_session = session_id or str(
        event.metadata.get("conversation_id") or f"{event.character_id}:default"
    )
    stage = time.perf_counter()
    if image_data_urls:
        model_call = runtime.model.react_call_with_images_for_session(
            context,
            image_data_urls,
            resolved_session,
        )
    else:
        model_call = runtime.model.react_call_for_session(context, resolved_session)
    reaction = model_call.value
    reaction, sticker_decisions, image_decisions = runtime._sanitize_resource_actions(
        reaction,
        allowed_sticker_ids=allowed_sticker_ids,
    )
    timings["model_ms"] = _ms(stage)

    return ReactionEvaluation(
        context=context,
        reaction=reaction,
        model_call=model_call,
        memories=memories,
        recent=recent,
        state_before=state_before,
        sticker_retrieval=sticker_retrieval,
        sticker_decisions=sticker_decisions,
        image_decisions=image_decisions,
        allowed_sticker_ids=allowed_sticker_ids,
        timings=timings,
    )
