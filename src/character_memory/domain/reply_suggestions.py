"""User reply drafts, never character actions or permission-bearing instructions."""
from uuid import UUID
from typing import Literal
from character_memory.domain.experience_profile import PreferenceChanges
from pydantic import BaseModel, ConfigDict, Field, model_validator
from character_memory.domain.facts import FactRef

class SuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    context_ref: FactRef
    expected_profile_version: int = Field(strict=True, ge=0, le=2**63-1)

class ReplySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    intent: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=800)

class SuggestionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: list[ReplySuggestion] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def different_choices(self):
        for name in ("intent", "text"):
            normalized = ["".join(getattr(item, name).split()).casefold() for item in self.suggestions]
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"suggestions need distinct {name}")
        return self


class SuggestionAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: UUID
    suggestion_id: UUID | None = None


class SuggestionFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    batch_id: UUID
    kind: Literal["IGNORE", "EXPLICIT_FEEDBACK"]
    comment: str | None = Field(default=None, max_length=2000)
    preference_changes: PreferenceChanges | None = None
    expected_profile_version: int | None = Field(default=None, strict=True, ge=0, le=2**63-2)

    @model_validator(mode="after")
    def explicit_feedback_contract(self):
        if self.kind == "IGNORE":
            if self.comment is not None or self.preference_changes is not None or self.expected_profile_version is not None:
                raise ValueError("IGNORE does not infer feedback or preferences")
        elif not (self.comment or self.preference_changes is not None):
            raise ValueError("explicit feedback requires text or explicit preference changes")
        if self.preference_changes is not None and self.expected_profile_version is None:
            raise ValueError("preference changes require expected_profile_version")
        return self
