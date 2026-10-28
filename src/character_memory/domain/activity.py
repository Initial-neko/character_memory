"""Activity lifecycle contract: durable scenario state and its participants.

P10 owns only creation, reading, pause, resume and end. It deliberately does not
execute a model, branch, replay or reflect. The registry is the single owner of
the activity state machine, its version and its activity-local identities, so a
later execution/reflection slice can reference these rows instead of inventing
another scenario store.

Two identities must never be conflated:

* a formal participant is an existing Person and reuses its ``character_id``;
* a simulated participant exists only inside this activity. Its participant id
  always carries the ``sim:`` prefix and it never owns a Persona, a registry
  entry or a second Person.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


MAX_ACTIVITY_VERSION = 2**31 - 1
MAX_ACTIVITY_PARTICIPANTS = 8
SIMULATED_ID_PREFIX = "sim:"

ActivityStatus = Literal["CREATED", "ACTIVE", "PAUSED", "ENDED"]
ActivityOperation = Literal["PAUSE", "RESUME", "END"]
RequestId = Annotated[StrictStr, Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
LocalId = Annotated[StrictStr, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]

# ENDED is terminal. RESUME is also the first activation of a CREATED activity,
# which is why creation itself returns CREATED instead of silently starting it.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"ACTIVE", "ENDED"}),
    "ACTIVE": frozenset({"PAUSED", "ENDED"}),
    "PAUSED": frozenset({"ACTIVE", "ENDED"}),
    "ENDED": frozenset(),
}

OPERATION_TARGET: dict[str, str] = {"PAUSE": "PAUSED", "RESUME": "ACTIVE", "END": "ENDED"}


class CharacterParticipant(BaseModel):
    """A participant that already exists as a formal Person."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_type: Literal["CHARACTER"]
    character_id: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("character_id")
    @classmethod
    def clean_character_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("character_id must not be blank")
        if value.startswith(SIMULATED_ID_PREFIX):
            # The prefix is the activity-local namespace marker; a formal id
            # carrying it would be indistinguishable from a simulated actor.
            raise ValueError("character_id must not use the simulated participant namespace")
        return value


class SimulatedParticipant(BaseModel):
    """An activity-local identity; it never becomes a Person.

    ``display_name`` is caller-supplied because the server must not invent a
    formal identity for a simulated actor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_type: Literal["SIMULATED_CHARACTER"]
    local_id: LocalId
    display_name: StrictStr = Field(min_length=1, max_length=60)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value


Participant = Annotated[
    CharacterParticipant | SimulatedParticipant,
    Field(discriminator="actor_type"),
]


def simulated_participant_id(local_id: str) -> str:
    return f"{SIMULATED_ID_PREFIX}{local_id}"


def participant_id(participant: CharacterParticipant | SimulatedParticipant) -> str:
    if participant.actor_type == "CHARACTER":
        return participant.character_id
    return simulated_participant_id(participant.local_id)


def participant_display_name(participant: CharacterParticipant | SimulatedParticipant) -> str:
    if participant.actor_type == "CHARACTER":
        return participant.character_id
    return participant.display_name


def transition_target(status: str, operation: str) -> str:
    """Return the target status, or raise ``ValueError`` for an illegal move.

    The repository turns this into the durable ``ACTIVITY_STATE_CONFLICT`` code;
    this function stays pure so the state machine is testable without SQLite.
    """
    if status not in LEGAL_TRANSITIONS:
        raise ValueError(f"unknown activity status: {status!r}")
    target = OPERATION_TARGET.get(operation)
    if target is None:
        raise ValueError(f"unknown activity operation: {operation!r}")
    if target not in LEGAL_TRANSITIONS[status]:
        raise ValueError(f"activity cannot {operation} from {status}")
    return target


class ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    title: StrictStr = Field(min_length=1, max_length=160)
    participants: list[Participant] = Field(min_length=1, max_length=MAX_ACTIVITY_PARTICIPANTS)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def unique_identities(self):
        keys = [participant_id(item) for item in self.participants]
        if len(keys) != len(set(keys)):
            raise ValueError("participants must be unique")
        formal = {item.character_id for item in self.participants if item.actor_type == "CHARACTER"}
        local = {item.local_id for item in self.participants if item.actor_type == "SIMULATED_CHARACTER"}
        if formal & local:
            raise ValueError("a simulated local_id must not reuse a formal character_id")
        return self


class ActivityAction(BaseModel):
    """One lifecycle action from the local operator or another local client.

    ``expected_version`` is mandatory so a stale reader can never overwrite a
    newer state; the service answers 409 instead of guessing. The operation
    itself is carried by the URL, so a request id and an operation cannot
    disagree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    expected_version: int = Field(strict=True, ge=1, le=MAX_ACTIVITY_VERSION)


def activity_experience_scope(activity_id: str, *, branch_id: str | None = None) -> dict:
    """The exact scope every fact produced by an activity must carry.

    Any Activity in this slice is simulated practice/scenario content. The
    existing ``domain.experience_scope`` boundary rejects it as real context, so
    this function returning ``SIMULATED`` is what keeps activity facts out of
    Direct/Group cognition. ``branch_id`` is reserved for P12 and is omitted
    until a real branch exists.
    """
    if not isinstance(activity_id, str) or not activity_id.strip() or len(activity_id) > 80:
        raise ValueError("invalid activity id for experience scope")
    scope: dict = {
        "schema_version": 1,
        "reality": "SIMULATED",
        "activity_id": activity_id.strip(),
    }
    if branch_id is not None:
        if not isinstance(branch_id, str) or not branch_id.strip() or len(branch_id) > 80:
            raise ValueError("invalid branch id for experience scope")
        scope["branch_id"] = branch_id.strip()
    return scope
