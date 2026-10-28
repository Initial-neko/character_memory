"""User-authored relationship practice: a SIMULATED Activity with 2-3 partners.

This is the P11 execution slice on top of the P10 Activity lifecycle. A scenario
reuses the existing Activity row for identity, status and version, and adds only
what a practice needs: the user's situation/goal/concern, partner descriptions,
and the practice transcript.

The boundary is the same one P10 froze: every fact in here is SIMULATED and
belongs to one ``activity_id``. Nothing in this module can write an Event,
Memory, Direct/Group conversation fact or a second Person.

A scenario needs at least one simulated participant. Formal characters give
their own perspective; a simulated participant is the practice counterpart and
also answers in role. Requiring one simulated participant is what keeps the
purpose explicit instead of turning an ordinary Group chat into a "scenario".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from character_memory.domain.activity import (
    ActivityCreate,
    Participant,
    RequestId,
    participant_id,
)
from character_memory.domain.reply_suggestions import SuggestionAttribution


MAX_SCENARIO_PARTICIPANTS = 3
MIN_SCENARIO_PARTICIPANTS = 2
MAX_PARTICIPANT_DESCRIPTION = 400

ScenarioEventType = Literal["USER_MESSAGE", "PERSPECTIVE", "REPLY"]
ScenarioActorType = Literal["USER", "CHARACTER", "SIMULATED_CHARACTER"]
ScenarioRoundStatus = Literal["PENDING", "READY", "FAILED"]
ScenarioReviewStatus = Literal["NONE", "PENDING", "READY", "FAILED"]


def normalized_text(value: str) -> str:
    return "".join(str(value).split()).casefold()


class ScenarioCreate(BaseModel):
    """Create one practice scene; the Activity owns status, version and scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    title: StrictStr = Field(min_length=1, max_length=160)
    situation: StrictStr = Field(min_length=1, max_length=2000)
    goal: StrictStr = Field(min_length=1, max_length=1000)
    concern: StrictStr | None = Field(default=None, max_length=1000)
    participants: list[Participant] = Field(
        min_length=MIN_SCENARIO_PARTICIPANTS, max_length=MAX_SCENARIO_PARTICIPANTS)
    descriptions: dict[str, StrictStr] = Field(default_factory=dict)

    @field_validator("title", "situation", "goal")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("scenario text must not be blank")
        return value

    @field_validator("concern")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("descriptions")
    @classmethod
    def clean_descriptions(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, text in value.items():
            key = str(key).strip()
            text = str(text).strip()
            if not key or not text:
                continue
            if len(key) > 80 or len(text) > MAX_PARTICIPANT_DESCRIPTION:
                raise ValueError("participant description is too long")
            cleaned[key] = text
        return cleaned

    @model_validator(mode="after")
    def scenario_cast(self):
        keys = [participant_id(item) for item in self.participants]
        if len(keys) != len(set(keys)):
            raise ValueError("participants must be unique")
        if not any(item.actor_type == "CHARACTER" for item in self.participants):
            raise ValueError("a relationship scenario requires a formal character participant")
        if not any(item.actor_type == "SIMULATED_CHARACTER" for item in self.participants):
            raise ValueError("a relationship scenario requires a simulated practice partner")
        known = set(keys)
        unknown = sorted(set(self.descriptions) - known)
        if unknown:
            raise ValueError(f"descriptions name unknown participants: {', '.join(unknown)}")
        return self

    def to_activity(self) -> ActivityCreate:
        return ActivityCreate(
            request_id=self.request_id,
            title=self.title,
            participants=list(self.participants),
        )


class ScenarioRoundRequest(BaseModel):
    """One user turn in the practice.

    ``expected_version`` is not used: the Activity version belongs to lifecycle
    actions only, so a message can never be mistaken for a state transition.
    Idempotency is the request id, exactly like the other accepted-fact paths.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    message: StrictStr = Field(min_length=1, max_length=4000)
    # The same attribution contract as ordinary chat: the client names a batch
    # and optionally the chosen draft; the server classifies SELECT/EDIT/FREE_INPUT
    # after the text is accepted. Practice signals stay in the existing signal
    # ledger, with a SCENARIO_EVENT source ref.
    reply_suggestion: SuggestionAttribution | None = None

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class PartnerPerspective(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_id: StrictStr = Field(min_length=1, max_length=80)
    stance: StrictStr = Field(min_length=1, max_length=40)
    perspective: StrictStr = Field(min_length=1, max_length=1200)


class SimulatedReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_id: StrictStr = Field(min_length=1, max_length=80)
    message: StrictStr = Field(min_length=1, max_length=800)


class ScenarioRoundOutput(BaseModel):
    """Structured partner output; distinctness is enforced, agreement is not.

    Every partner must contribute their own perspective, and no two may be the
    same words. Disagreement is legitimate: nothing here asks them to converge
    on one answer. Replies are limited to simulated participants because only a
    simulated counterpart is answering the user inside the practice.
    """

    model_config = ConfigDict(extra="forbid")

    perspectives: list[PartnerPerspective] = Field(
        min_length=MIN_SCENARIO_PARTICIPANTS, max_length=MAX_SCENARIO_PARTICIPANTS)
    replies: list[SimulatedReply] = Field(default_factory=list, max_length=MAX_SCENARIO_PARTICIPANTS)

    @model_validator(mode="after")
    def distinguishable_views(self):
        ids = [item.participant_id for item in self.perspectives]
        if len(ids) != len(set(ids)):
            raise ValueError("each partner gives exactly one perspective")
        for name in ("stance", "perspective"):
            values = [normalized_text(getattr(item, name)) for item in self.perspectives]
            if len(values) != len(set(values)):
                raise ValueError(f"partner perspectives need distinct {name}")
        reply_ids = [item.participant_id for item in self.replies]
        if len(reply_ids) != len(set(reply_ids)):
            raise ValueError("a simulated participant replies at most once per turn")
        return self


def validate_round_output(output: ScenarioRoundOutput, *, participant_ids: list[str],
                          simulated_ids: list[str]) -> ScenarioRoundOutput:
    """Reject an answer that invents partner identity or answers for a formal person.

    Shape checks live on the model; identity checks need the scenario cast, so
    they run here and still fail the round explicitly instead of printing a
    plausible-looking but wrong transcript.
    """
    expected = set(participant_ids)
    seen = [item.participant_id for item in output.perspectives]
    if set(seen) != expected or len(seen) != len(expected):
        raise ValueError("perspectives must cover exactly the scenario participants")
    if any(item.participant_id not in simulated_ids for item in output.replies):
        raise ValueError("only simulated participants may reply inside the practice")
    return output


# ---------------------------------------------------------------------------
# P12: branches, review and explicit adoption
# ---------------------------------------------------------------------------

class ScenarioBranchCreate(BaseModel):
    """Create one imagined branch from an existing main-transcript event.

    ``after_event_id`` is the branch point: the model sees the main transcript
    only up to and including that event. The alternative message is the user's
    imagined next turn; later main turns are deliberately invisible to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    after_event_id: int = Field(strict=True, ge=1, le=2**63 - 1)
    message: StrictStr = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def clean_branch_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("branch message must not be blank")
        return value


class ScenarioBranchRetry(BaseModel):
    """Retry a durably failed branch with a fresh request id.

    Retrying is explicit because a failed generation is stored as failed: the
    same request id keeps returning the same failure, never a silent second
    model call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId


class ScenarioBranchOutput(ScenarioRoundOutput):
    """Partner views for the alternative turn plus an explicit consequence.

    The consequence is model-generated and SIMULATED. It is stored on the
    branch, never on a real Event/Memory, and it never claims the imagined path
    actually happened.
    """

    consequence: StrictStr = Field(min_length=1, max_length=1200)

    @field_validator("consequence")
    @classmethod
    def clean_consequence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("branch consequence must not be blank")
        return value


class ScenarioReviewRequest(BaseModel):
    """Generate (or retry) the review of one ended scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId


class ScenarioReviewOutput(BaseModel):
    """The review is structured so adoption can quote it without inventing parts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    overview: StrictStr = Field(min_length=1, max_length=1200)
    strategy: StrictStr = Field(min_length=1, max_length=800)
    feedback: StrictStr = Field(min_length=1, max_length=800)
    next_step: StrictStr | None = Field(default=None, max_length=600)

    @field_validator("next_step")
    @classmethod
    def clean_next_step(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ScenarioAdoptRequest(BaseModel):
    """Explicitly adopt one READY branch as a REAL shared experience.

    Adoption is the only operation in this slice that may write a REAL Event or
    Memory. ``character_id`` is optional when the scene has exactly one formal
    participant; with two, the caller must name which Person remembers it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    branch_id: StrictStr = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    character_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
