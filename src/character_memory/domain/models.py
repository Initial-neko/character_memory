from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator


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
    # V0 legacy decision names remain readable for old traces and callers.
    REPLY = "REPLY"
    MINIMAL_RESPONSE = "MINIMAL_RESPONSE"
    NO_REPLY = "NO_REPLY"
    DEFER = "DEFER"
    PROACTIVE_MESSAGE = "PROACTIVE_MESSAGE"
    NO_ACTION = "NO_ACTION"
    # P0 visible expression primitives.
    MESSAGE = "MESSAGE"
    EMOJI = "EMOJI"
    STICKER = "STICKER"
    IMAGE = "IMAGE"


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
    reason: str = ""
    # Some providers drift among `message`, `text` and `content` for textual
    # actions. Normalize those narrow aliases at the schema boundary while
    # keeping `message` as the only canonical persisted/API field.
    message: str | None = Field(default=None, validation_alias=AliasChoices("message", "text", "content"))
    sticker_id: str | None = None
    image_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_nullable_reason(cls, value):
        if isinstance(value, dict) and value.get("reason") is None:
            value = dict(value)
            value["reason"] = ""
        return value

    @model_validator(mode="after")
    def validate_message_contract(self):
        message_actions = {
            ActionType.REPLY,
            ActionType.MINIMAL_RESPONSE,
            ActionType.PROACTIVE_MESSAGE,
            ActionType.MESSAGE,
            ActionType.EMOJI,
        }
        if self.type in message_actions:
            if not (self.message or "").strip():
                raise ValueError(f"{self.type.value} requires a non-empty message")
            self.message = self.message.strip()
            self.sticker_id = None
            self.image_id = None
            return self
        if self.type == ActionType.STICKER:
            if not (self.sticker_id or "").strip():
                raise ValueError("STICKER requires sticker_id")
            self.message = None
            self.image_id = None
            self.sticker_id = self.sticker_id.strip()
            return self
        if self.type == ActionType.IMAGE:
            if not (self.image_id or "").strip():
                raise ValueError("IMAGE requires image_id")
            self.message = None
            self.sticker_id = None
            self.image_id = self.image_id.strip()
            return self
        self.message = None
        self.sticker_id = None
        self.image_id = None
        return self


class MemoryCandidate(BaseModel):
    content: str
    memory_type: str = "EPISODIC"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value):
        # Vision/text providers occasionally compress a simple memory object to
        # a bare string. Accept that narrow, lossless drift at the schema
        # boundary so one malformed optional candidate cannot fail the entire
        # reaction. Complex non-object shapes remain invalid.
        if isinstance(value, str):
            return {"content": value}
        return value


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
    # Internal/debug summaries are intentionally sparse. They are safe summaries,
    # never raw hidden chain-of-thought.
    perception: str = ""
    reaction: str = ""
    mental_state_update: str = ""

    # P0 primary contract: zero to three outward actions. [] means genuine silence.
    actions: list[ActionDecision] = Field(default_factory=list, max_length=3)

    # Legacy compatibility for existing callers/traces. Models do not need to emit
    # this field; it is normalized from actions. Old single-action JSON remains valid.
    action: ActionDecision | None = None

    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=6)
    intent_candidates: list[IntentCandidate] = Field(default_factory=list, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value):
        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        # Providers often use JSON null to mean "no update / no summary". Internally
        # these fields stay canonical strings so Runtime code never has to branch on None.
        for key in ("perception", "reaction", "mental_state_update"):
            if normalized.get(key) is None:
                normalized[key] = ""

        # The same applies to optional candidate arrays. Treat an explicit null as
        # an empty list, but still require an explicit action contract below.
        for key in ("actions", "memory_candidates", "intent_candidates"):
            if key in normalized and normalized[key] is None:
                normalized[key] = []

        if "actions" not in normalized and "action" not in normalized:
            raise ValueError("PersonReaction requires explicit actions (including []) or legacy action")
        return normalized

    @model_validator(mode="after")
    def normalize_action_contract(self):
        if self.actions:
            self.action = self.actions[0]
        elif self.action is not None:
            if self.action.type not in {ActionType.NO_REPLY, ActionType.NO_ACTION}:
                self.actions = [self.action]
        if self.action is None:
            self.action = ActionDecision(type=ActionType.NO_REPLY)
        return self


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
