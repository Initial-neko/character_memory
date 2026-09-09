from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class EventType(str, Enum):
    USER_MESSAGE = "USER_MESSAGE"
    CHARACTER_MESSAGE = "CHARACTER_MESSAGE"
    TIME_TICK = "TIME_TICK"
    LIFE_EVENT = "LIFE_EVENT"
    DIARY = "DIARY"
    SOCIAL_POST = "SOCIAL_POST"
    PROACTIVE_INTENT = "PROACTIVE_INTENT"
    ACTION = "ACTION"


class ActionType(str, Enum):
    REPLY = "REPLY"
    MINIMAL_RESPONSE = "MINIMAL_RESPONSE"
    NO_REPLY = "NO_REPLY"
    DEFER = "DEFER"
    PROACTIVE_MESSAGE = "PROACTIVE_MESSAGE"
    NO_ACTION = "NO_ACTION"


class Event(BaseModel):
    id: int | None = None
    character_id: str
    event_type: EventType
    event_time: datetime
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Memory(BaseModel):
    id: int | None = None
    character_id: str
    content: str
    memory_type: str = "EPISODIC"
    event_time: datetime
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_event_id: int | None = None
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None


class ActionDecision(BaseModel):
    type: ActionType
    reason: str
    message: str | None = None

    @model_validator(mode="after")
    def validate_message_contract(self):
        expressive = {ActionType.REPLY, ActionType.MINIMAL_RESPONSE, ActionType.PROACTIVE_MESSAGE}
        if self.type in expressive and not (self.message or "").strip():
            raise ValueError(f"{self.type.value} requires a non-empty message")
        if self.type not in expressive:
            self.message = None
        return self


class MemoryCandidate(BaseModel):
    content: str
    memory_type: str = "EPISODIC"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class IntentCandidate(BaseModel):
    content: str
    preferred_action: ActionType = ActionType.PROACTIVE_MESSAGE
    earliest_hours: float = Field(default=0, ge=0, le=720)
    expires_hours: float = Field(default=48, ge=0, le=720)

    @model_validator(mode="after")
    def validate_window(self):
        if self.expires_hours < self.earliest_hours:
            raise ValueError("expires_hours must be >= earliest_hours")
        return self


class PersonReaction(BaseModel):
    perception: str = Field(description="Concise perception summary, not chain-of-thought")
    reaction: str = Field(description="Short developer-safe reaction summary, not chain-of-thought")
    mental_state_update: str = Field(description="Complete compact mental state after this event")
    action: ActionDecision
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=6)
    intent_candidates: list[IntentCandidate] = Field(default_factory=list, max_length=4)


class RuntimeResult(BaseModel):
    event: Event
    recalled_memories: list[Memory]
    reaction: PersonReaction
    context: str = ""
    mental_state_before: str = ""
    created_memory_ids: list[int] = Field(default_factory=list)
    created_intent_ids: list[int] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class LifeEventCandidate(BaseModel):
    content: str
    importance: float = Field(default=0.4, ge=0.0, le=1.0)
    hour: int | None = Field(default=None, ge=0, le=23)


class DailyLifePlan(BaseModel):
    events: list[LifeEventCandidate] = Field(default_factory=list, max_length=3)
    social_post: str | None = None
    image_prompt: str | None = None


class DiaryResult(BaseModel):
    diary: str
    mental_state_update: str
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=6)
