from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from character_memory.domain.models import Event, EventType, Memory
from character_memory.time_utils import epoch_us
from character_memory.domain.experience_scope import is_real_experience


@dataclass(frozen=True)
class PersonContextSnapshot:
    persona: str
    mental_state: str
    memories: list[Memory]
    recent_events: list[Event]
    observed_events: list[Event] = field(default_factory=list)


class PersonContextBuilder:
    """Shared read-only person context for Direct, Group, Space and World planning.

    It deliberately does not decide actions or persistence. Channels still own
    their own behavioral contracts; this only prevents each channel from
    inventing a different way to read the same Person.
    """

    def __init__(self, store, recall, persona: str):
        self.store = store
        self.recall = recall
        self.persona = persona

    def build(
        self,
        character_id: str,
        *,
        query: str,
        at: datetime,
        recent_events: list[Event] | None = None,
        exclude_event_id: int | None = None,
        recent_limit: int = 8,
        observed_projection: str = "PERSONAL",
    ) -> PersonContextSnapshot:
        memories = self.recall.recall(character_id, str(query or "").strip() or "recent personal context", now=at)
        if recent_events is None:
            recent = self.store.list_events(
                character_id, limit=max(1, recent_limit + 2), before=at,
                exclude_reaction_receipts=True, real_experience_only=True,
            )
            if exclude_event_id is not None:
                recent = [item for item in recent if item.id != exclude_event_id]
            recent = recent[-recent_limit:]
        else:
            recent = [
                item for item in recent_events
                if not (
                    item.event_type == EventType.ACTION
                    and item.metadata.get("trace_id") is not None
                    and item.metadata.get("fact_kind") != "ROLE_COLLABORATION_MESSAGE"
                )
                and is_real_experience(item.metadata)
                and epoch_us(item.event_time) <= epoch_us(at)
                and (exclude_event_id is None or item.id != exclude_event_id)
            ][-recent_limit:]
        observed = self.store.recall_observed_events(character_id, query, at=at, projection=observed_projection)
        return PersonContextSnapshot(
            persona=self.persona,
            mental_state=self.store.get_mental_state(character_id, at=at),
            memories=[m for m in memories if is_real_experience(m.metadata)],
            recent_events=recent,
            observed_events=[e for e in observed if is_real_experience(e.metadata)],
        )
