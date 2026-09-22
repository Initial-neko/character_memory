from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from character_memory.domain.models import Event, Memory


@dataclass(frozen=True)
class PersonContextSnapshot:
    persona: str
    mental_state: str
    memories: list[Memory]
    recent_events: list[Event]


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
    ) -> PersonContextSnapshot:
        memories = self.recall.recall(character_id, str(query or "").strip() or "recent personal context", now=at)
        if recent_events is None:
            recent = self.store.list_events(character_id, limit=max(1, recent_limit + 2), before=at)
            if exclude_event_id is not None:
                recent = [item for item in recent if item.id != exclude_event_id]
            recent = recent[-recent_limit:]
        else:
            recent = list(recent_events)[-recent_limit:]
        return PersonContextSnapshot(
            persona=self.persona,
            mental_state=self.store.get_mental_state(character_id, at=at),
            memories=memories,
            recent_events=recent,
        )
