"""Typed references to existing authoritative fact stores; no copied facts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["EVENT", "CONVERSATION_EVENT"]
    id: int = Field(strict=True, gt=0, le=2**63 - 1)
    conversation_id: str | None = Field(default=None, strict=True, min_length=1)

    @model_validator(mode="after")
    def validate_domain(self):
        if self.kind == "CONVERSATION_EVENT":
            if self.conversation_id is None or not self.conversation_id.strip():
                raise ValueError("conversation fact requires conversation_id")
        elif self.conversation_id is not None:
            raise ValueError("core event reference cannot use a conversation namespace")
        return self
