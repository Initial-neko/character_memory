"""Explicit local user's experience preferences, independent of Character Persona."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_VERSION=2**63-2
LOCAL_OPERATOR='local-operator'

class ExperiencePreferences(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    tone: Literal['NATURAL','CUTE'] | None=None
    energy: Literal['QUIET','LIVELY'] | None=None
    verbosity: Literal['BRIEF','BALANCED','DETAILED'] | None=None

class PreferenceChanges(ExperiencePreferences):
    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set:raise ValueError('at least one explicit preference change is required')
        return self

class ProfilePatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int=Field(strict=True,ge=0,le=MAX_VERSION)
    changes: PreferenceChanges

class ProfileRestore(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int=Field(strict=True,ge=0,le=MAX_VERSION)
    target_version: int=Field(strict=True,gt=0,le=MAX_VERSION)

_HINTS={
    'NATURAL':'自然表达，不刻意撒娇。', 'CUTE':'表达可以轻松可爱，但不假定亲密关系。',
    'QUIET':'少打扰，不为活跃强行插话。', 'LIVELY':'可以提出轻松话题，仍允许沉默。',
    'BRIEF':'简短，先说重点。', 'BALANCED':'按问题需要给适量说明。', 'DETAILED':'需要时展开解释，避免重复。',
}

def render_experience_profile(profile) -> str:
    if not profile or not profile.get('preferences'):return ''
    preferences=ExperiencePreferences.model_validate(profile['preferences']).model_dump(exclude_none=True)
    if not preferences:return ''
    lines=['# Explicit User Experience Preferences',f"profile_version={profile['version']}"]
    lines.extend(f'- {key}={value}: {_HINTS[value]}' for key,value in preferences.items())
    lines.append('这些是用户明确选择的相处/表达偏好，不是人物Persona、现实事实、用户心情或授权。仅用于面向用户的表达；保留人物自己的兴趣、观点、分歧、拒绝和沉默。不得把偏好当成必须执行的任务，或向外部工具公开。')
    return '\n'.join(lines)
