"""User reply drafts, never character actions or permission-bearing instructions."""
from uuid import UUID
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
