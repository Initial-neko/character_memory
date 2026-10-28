"""Typed references to existing authoritative fact stores; no copied facts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class FactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["EVENT", "CONVERSATION_EVENT", "SCENARIO_EVENT"]
    id: int = Field(strict=True, gt=0, le=2**63 - 1)
    conversation_id: str | None = Field(default=None, strict=True, min_length=1)
    activity_id: str | None = Field(default=None, strict=True, min_length=1, max_length=80)

    @model_serializer(mode="wrap")
    def omit_empty_activity(self, handler):
        # Existing Direct/Group references serialized before P11 must keep the
        # same shape. The new namespace is emitted only when it actually names
        # a practice fact.
        data = handler(self)
        if isinstance(data, dict) and data.get("activity_id") is None:
            data.pop("activity_id", None)
        return data

    @model_validator(mode="after")
    def validate_domain(self):
        if self.kind == "CONVERSATION_EVENT":
            if self.conversation_id is None or not self.conversation_id.strip():
                raise ValueError("conversation fact requires conversation_id")
            if self.activity_id is not None:
                raise ValueError("conversation fact cannot use an activity namespace")
        elif self.kind == "SCENARIO_EVENT":
            # A practice fact is identified by its Activity, never by a real
            # Direct/Group conversation. Keeping the two namespaces disjoint is
            # what stops a simulated event from being resolved as a real one.
            if self.activity_id is None or not self.activity_id.strip():
                raise ValueError("scenario fact requires activity_id")
            if self.conversation_id is not None:
                raise ValueError("scenario fact cannot use a conversation namespace")
        else:
            if self.conversation_id is not None:
                raise ValueError("core event reference cannot use a conversation namespace")
            if self.activity_id is not None:
                raise ValueError("core event reference cannot use an activity namespace")
        return self
