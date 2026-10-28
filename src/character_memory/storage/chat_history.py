from __future__ import annotations

from dataclasses import dataclass
import json

from character_memory.domain.models import Event, EventType
from character_memory.storage.conversation_facts import ConversationFactReader
from character_memory.time_utils import parse_datetime


@dataclass(frozen=True)
class ChatHistoryPage:
    events: list[Event]
    has_more: bool
    next_before_id: int | None


class ChatHistoryRepository:
    """Read-optimized Direct chat history over the unified conversation facts.

    Conversation facts have one home after W3. This repository delegates to the
    shared reader instead of knowing table layouts, so history, search, SSE
    reconciliation and voice all page over the same rows.
    """

    def __init__(self, store):
        self.store = store
        self.facts = ConversationFactReader(store)

    def list_page(self, character_id: str, *, limit: int = 50, before_id: int | None = None) -> ChatHistoryPage:
        return self.facts.list_page(character_id, limit=limit, before_id=before_id)

    def trace_sources(self, character_id: str, source_event_ids: list[int]) -> set[int]:
        return self.facts.trace_sources(character_id, source_event_ids)
