from __future__ import annotations

from datetime import datetime
from enum import Enum
import math
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator


class EventType(str, Enum):
    USER_MESSAGE = "USER_MESSAGE"
    CHARACTER_MESSAGE = "CHARACTER_MESSAGE"
    TIME_TICK = "TIME_TICK"
    LIFE_EVENT = "LIFE_EVENT"
    DIARY = "DIARY"
    SOCIAL_POST = "SOCIAL_POST"
    WORLD_OBSERVATION = "WORLD_OBSERVATION"
    SPACE_POST_SEEN = "SPACE_POST_SEEN"
    SPACE_COMMENT_RECEIVED = "SPACE_COMMENT_RECEIVED"
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
    # A spoken chat message: the text is still the message body (shown when the
    # bubble is expanded), with generated audio attached via event metadata.
    VOICE_MESSAGE = "VOICE_MESSAGE"
    # P0.19 internal visual-tool intent. This is not a visible chat message by
    # itself; direct-chat orchestration may turn it into a generated IMAGE event.
    GENERATE_IMAGE = "GENERATE_IMAGE"
    # Space-only social actions. They are intentionally not part of
    # EXPRESSIVE_ACTIONS, so PersonRuntime never persists them as chat messages.
    SPACE_LIKE = "SPACE_LIKE"
    SPACE_COMMENT = "SPACE_COMMENT"


# Actions that produce a visible outward message. All four action gates --
# runtime/person_runtime.py, application/group_conversation_service.py,
# application/proactive_service.py and eval/runner.py -- consult this one set.
# It used to be duplicated verbatim in each of them, where editing one copy
# silently dropped the action in the other three.
EXPRESSIVE_ACTIONS = frozenset({
    ActionType.REPLY,
    ActionType.MINIMAL_RESPONSE,
    ActionType.PROACTIVE_MESSAGE,
    ActionType.MESSAGE,
    ActionType.VOICE_MESSAGE,
    ActionType.EMOJI,
    ActionType.STICKER,
    ActionType.IMAGE,
})


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


def _bounded_float(value, *, default: float, minimum: float, maximum: float) -> float:
    """Coerce a soft LLM score/window without letting auxiliary metadata kill a reply."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


class ActionDecision(BaseModel):
    type: ActionType
    reason: str = ""
    # Some providers drift among `message`, `text` and `content` for textual
    # actions. Normalize those narrow aliases at the schema boundary while
    # keeping `message` as the only canonical persisted/API field.
    message: str | None = Field(default=None, validation_alias=AliasChoices("message", "text", "content"))
    sticker_id: str | None = None
    image_id: str | None = None
    image_purpose: str | None = None
    visual_intent: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        # A common JSON-model drift is to emit {"action":"MESSAGE"} instead
        # of the canonical {"type":"MESSAGE"}. The meaning is unambiguous, so
        # normalize it locally instead of paying for a second LLM repair call.
        if "type" not in normalized and normalized.get("action") is not None:
            normalized["type"] = normalized.get("action")
        raw_type = normalized.get("type")
        if isinstance(raw_type, str):
            normalized["type"] = raw_type.strip().upper()
        if normalized.get("reason") is None:
            normalized["reason"] = ""
        return normalized

    @model_validator(mode="after")
    def validate_message_contract(self):
        message_actions = {
            ActionType.REPLY,
            ActionType.MINIMAL_RESPONSE,
            ActionType.PROACTIVE_MESSAGE,
            ActionType.MESSAGE,
            ActionType.VOICE_MESSAGE,
            ActionType.EMOJI,
            ActionType.SPACE_COMMENT,
        }
        if self.type in message_actions:
            if not (self.message or "").strip():
                raise ValueError(f"{self.type.value} requires a non-empty message")
            self.message = self.message.strip()
            self.sticker_id = None
            self.image_id = None
            self.image_purpose = None
            self.visual_intent = None
            return self
        if self.type == ActionType.STICKER:
            if not (self.sticker_id or "").strip():
                raise ValueError("STICKER requires sticker_id")
            self.message = None
            self.image_id = None
            self.image_purpose = None
            self.visual_intent = None
            self.sticker_id = self.sticker_id.strip()
            return self
        if self.type == ActionType.IMAGE:
            if not (self.image_id or "").strip():
                raise ValueError("IMAGE requires image_id")
            self.message = None
            self.sticker_id = None
            self.image_purpose = None
            self.visual_intent = None
            self.image_id = self.image_id.strip()
            return self
        if self.type == ActionType.GENERATE_IMAGE:
            purpose = str(self.image_purpose or "").strip().upper()
            intent = str(self.visual_intent or "").strip()
            if purpose not in {"SELFIE", "SCENE"}:
                raise ValueError("GENERATE_IMAGE image_purpose must be SELFIE or SCENE")
            if not intent:
                raise ValueError("GENERATE_IMAGE requires visual_intent")
            self.message = None
            self.sticker_id = None
            self.image_id = None
            self.image_purpose = purpose
            self.visual_intent = intent[:800]
            return self
        self.message = None
        self.sticker_id = None
        self.image_id = None
        self.image_purpose = None
        self.visual_intent = None
        return self


class MemoryCandidate(BaseModel):
    content: str
    memory_type: str = "EPISODIC"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value):
        # Memory is auxiliary to the outward reaction. Providers sometimes emit
        # a 1~5 style score, numeric strings, null types, or a bare memory string.
        # Normalize harmless drift here so a good MESSAGE is never discarded just
        # because its optional memory annotation was a little sloppy.
        if isinstance(value, str):
            return {"content": value}
        if isinstance(value, dict):
            normalized = dict(value)
            if not str(normalized.get("memory_type") or "").strip():
                normalized["memory_type"] = "EPISODIC"
            normalized["importance"] = _bounded_float(
                normalized.get("importance", 0.5),
                default=0.5,
                minimum=0.0,
                maximum=1.0,
            )
            return normalized
        return value


class IntentCandidate(BaseModel):
    content: str
    preferred_action: ActionType = ActionType.PROACTIVE_MESSAGE
    earliest_hours: float = Field(default=0, ge=0, le=720)
    expires_hours: float = Field(default=48, ge=0, le=720)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value):
        if isinstance(value, str):
            return {"content": value}
        if isinstance(value, dict):
            normalized = dict(value)
            raw_action = normalized.get("preferred_action", ActionType.PROACTIVE_MESSAGE.value)
            try:
                normalized["preferred_action"] = ActionType(raw_action)
            except (TypeError, ValueError):
                normalized["preferred_action"] = ActionType.PROACTIVE_MESSAGE
            earliest = _bounded_float(
                normalized.get("earliest_hours", 0),
                default=0.0,
                minimum=0.0,
                maximum=720.0,
            )
            expires = _bounded_float(
                normalized.get("expires_hours", 48),
                default=48.0,
                minimum=0.0,
                maximum=720.0,
            )
            normalized["earliest_hours"] = earliest
            normalized["expires_hours"] = max(earliest, expires)
            return normalized
        return value

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

    # P0 primary contract: zero to three outward/tool actions. [] means genuine silence.
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
        # Debug summaries are auxiliary; scalar provider drift should never take
        # down an otherwise valid outward action.
        for key in ("perception", "reaction", "mental_state_update"):
            raw = normalized.get(key)
            if raw is None:
                normalized[key] = ""
            elif not isinstance(raw, str):
                normalized[key] = str(raw)

        # Candidate lists are optional side effects. Validate each candidate in
        # isolation and drop only the malformed candidate instead of invalidating
        # the whole PersonReaction and losing a valid outward reply.
        raw_memories = normalized.get("memory_candidates", [])
        if not isinstance(raw_memories, list):
            raw_memories = []
        memories: list[MemoryCandidate] = []
        for candidate in raw_memories[:6]:
            try:
                memories.append(MemoryCandidate.model_validate(candidate))
            except (ValidationError, TypeError, ValueError):
                continue
        normalized["memory_candidates"] = memories

        raw_intents = normalized.get("intent_candidates", [])
        if not isinstance(raw_intents, list):
            raw_intents = []
        intents: list[IntentCandidate] = []
        for candidate in raw_intents[:4]:
            try:
                intents.append(IntentCandidate.model_validate(candidate))
            except (ValidationError, TypeError, ValueError):
                continue
        normalized["intent_candidates"] = intents

        # Keep the outward action contract strict. If actions themselves are bad,
        # the repair path still gets a chance; only optional metadata is softened.
        if "actions" in normalized and normalized["actions"] is None:
            normalized["actions"] = []
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
