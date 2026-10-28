"""Interpretable preference candidates from authored feedback; never auto-applied.

Only the existing tone/energy/verbosity fields may be proposed. The interpreter is
deterministic and returns the exact phrase it matched, so the operator can compare
and reject a suggestion without trusting an opaque inference. A candidate is not a
profile change: applying it still goes through the explicit Profile version + CAS.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from character_memory.domain.experience_profile import ExperiencePreferences, MAX_VERSION

CandidateScope = Literal['LONG_TERM', 'SCENE']
CandidateStatus = Literal['PENDING', 'APPLIED', 'REVERTED', 'DISMISSED', 'SATISFIED', 'OVERRIDDEN', 'SUPERSEDED', 'WITHDRAWN']
_FIELD_ORDER = ('tone', 'energy', 'verbosity')


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    field: Literal['tone', 'energy', 'verbosity']
    value: str
    phrase: str


class PreferenceInterpretation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: CandidateScope
    fields: dict[str, str]
    evidence: list[CandidateEvidence]
    note: str

    @field_validator('fields')
    @classmethod
    def only_supported_fields(cls, value):
        normalized = ExperiencePreferences.model_validate(value).model_dump(exclude_none=True)
        if not normalized:
            raise ValueError('an interpretation needs at least one supported preference field')
        return {key: normalized[key] for key in _FIELD_ORDER if key in normalized}


class CandidateApply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str
    expected_profile_version: int = Field(strict=True, ge=0, le=MAX_VERSION)
    # A scene-scoped inference only reaches the shared profile after the user
    # explicitly says it should also apply in the long term.
    as_long_term: bool = Field(default=False, strict=True)

    @field_validator('request_id')
    @classmethod
    def valid_request_id(cls, value):
        from uuid import UUID
        try:
            UUID(str(value))
        except (TypeError, ValueError):
            raise ValueError('request_id must be a UUID') from None
        return str(value)


class CandidateDismiss(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str

    @field_validator('request_id')
    @classmethod
    def valid_request_id(cls, value):
        from uuid import UUID
        try:
            UUID(str(value))
        except (TypeError, ValueError):
            raise ValueError('request_id must be a UUID') from None
        return str(value)


class CandidateRevert(CandidateDismiss):
    model_config = ConfigDict(extra='forbid')
    expected_profile_version: int = Field(strict=True, ge=0, le=MAX_VERSION)


# Longer phrases win, so "别太可爱" resolves to NATURAL instead of CUTE and
# "别太长" resolves to BRIEF. Ties across values in one field are ambiguous and
# produce no candidate for that field.
_TERMS = {
    'tone': (
        ('NATURAL', ('不要太可爱', '别太可爱', '不要撒娇', '别撒娇', '不要卖萌', '别卖萌',
                     '自然一点', '自然', '正常一点', 'not too cute', 'natural', 'normal')),
        ('CUTE', ('可爱一点', '可爱', '撒娇', '卖萌', '甜一点', 'cute', 'sweeter', 'sweet')),
    ),
    'energy': (
        ('QUIET', ('别太热闹', '不要太热闹', '少说点', '少打扰', '别插话', '安静一点', '安静',
                   'quiet', 'calmer', 'less chatty')),
        ('LIVELY', ('热闹一点', '热闹', '活泼', '活跃', '多聊', 'lively', 'livelier', 'energetic', 'playful')),
    ),
    'verbosity': (
        ('BRIEF', ('不要太详细', '别太详细', '不要那么长', '不要太长', '别太长',
                   '简短一点', '简短', '短一点', '短些', '简洁', '简略', '说少点',
                   'brief', 'shorter', 'concise', 'short')),
        ('DETAILED', ('详细一点', '详细', '丰富一点', '丰富', '展开说', '展开', '多解释', '多讲',
                      'detailed', 'more detail', 'elaborate', 'longer')),
        ('BALANCED', ('适中', '刚刚好', 'balanced', 'moderate')),
    ),
}
_SCENE_MARKERS = ('这次', '刚才', '这一条', '这一句', '这条回复', '这一回', '临时', '先这样',
                  'this time', 'for now', 'just this', 'just now')
_LONG_MARKERS = ('以后', '今后', '接下来', '从现在起', '从现在开始', '每次', '长期', '一直', '总是',
                 'always', 'from now on', 'going forward', 'in the future')
_NOTES = {
    'LONG_TERM': '这段反馈被理解为长期偏好候选；只有你确认采用后才会保存为新版本。',
    'SCENE': '这段反馈被理解为只针对当前场景；只有你明确确认长期采用后才会保存。',
}


def _best_match(text, options):
    best = {}
    for value, phrases in options:
        for phrase in phrases:
            if phrase in text and (value not in best or len(phrase) > len(best[value])):
                best[value] = phrase
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda item: len(item[1]), reverse=True)
    if len(ranked) > 1 and len(ranked[0][1]) == len(ranked[1][1]):
        return None
    return ranked[0]


def candidate_scope(text):
    normalized = text.casefold()
    if any(marker in normalized for marker in _LONG_MARKERS):
        return 'LONG_TERM'
    # No explicit time marker stays scene-scoped: an unstated inference must not
    # become a durable choice without the operator's confirmation.
    return 'SCENE'


def interpret_feedback(text):
    """Return a bounded, explainable interpretation or None; never writes anything."""
    normalized = str(text or '').casefold()
    if not normalized.strip():
        return None
    fields, evidence = {}, []
    for field in _FIELD_ORDER:
        match = _best_match(normalized, _TERMS[field])
        if match is None:
            continue
        value, phrase = match
        fields[field] = value
        evidence.append(CandidateEvidence(field=field, value=value, phrase=phrase))
    if not fields:
        return None
    scope = candidate_scope(normalized)
    return PreferenceInterpretation(scope=scope, fields=fields, evidence=evidence, note=_NOTES[scope])


def candidate_fields(interpretation, current_preferences):
    """Split interpretation fields by explicit-choice precedence.

    An explicit choice always wins: a field the operator already set is never
    overwritten, whether its value matches (satisfied) or differs (overridden).
    """
    applicable, satisfied, overridden = {}, [], []
    for field, value in interpretation.fields.items():
        if field not in current_preferences:
            applicable[field] = value
        elif current_preferences[field] == value:
            satisfied.append(field)
        else:
            overridden.append(field)
    return applicable, satisfied, overridden
