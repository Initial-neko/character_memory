"""Operator-confirmed long-term Person goals; no model or Intent policy."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator
from character_memory.domain.facts import FactRef

MAX_GOAL_VERSION=2**31-1
RequestId=Annotated[StrictStr,Field(min_length=8,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')]
Stage=Annotated[StrictStr,Field(min_length=1,max_length=240)]

class GoalCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    request_id: RequestId
    title: StrictStr=Field(min_length=1,max_length=160)
    success_criteria: StrictStr=Field(min_length=1,max_length=1000)
    stages: list[Stage]=Field(min_length=1,max_length=8)

    @field_validator('title','success_criteria')
    @classmethod
    def clean_text(cls,value):
        value=value.strip()
        if not value:raise ValueError('text must not be blank')
        return value

    @field_validator('stages')
    @classmethod
    def clean_stages(cls,values):
        values=[x.strip() for x in values]
        if not all(values):raise ValueError('stages must not be blank')
        return values

class GoalAction(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    request_id: RequestId
    expected_version: int=Field(strict=True,ge=1,le=MAX_GOAL_VERSION)
    operation: Literal['PAUSE','RESUME','COMPLETE_STAGE','ARCHIVE']
    confirmation: Literal['OPERATOR_ACCEPTED'] | None=None
    evidence: list[FactRef]=Field(default_factory=list,max_length=8)
    note: StrictStr | None=Field(default=None,max_length=500)

    @field_validator('note')
    @classmethod
    def clean_note(cls,value):
        if value is None:return None
        value=value.strip()
        if not value:raise ValueError('note must not be blank')
        return value

    @model_validator(mode='after')
    def validate_completion(self):
        if self.operation=='COMPLETE_STAGE':
            if self.confirmation!='OPERATOR_ACCEPTED' or not self.evidence:
                raise ValueError('completion requires explicit acceptance and source references')
            if len({x.model_dump_json() for x in self.evidence})!=len(self.evidence):
                raise ValueError('duplicate evidence reference')
        elif self.confirmation is not None or self.evidence or self.note is not None:
            raise ValueError('only stage completion accepts evidence or confirmation')
        return self
