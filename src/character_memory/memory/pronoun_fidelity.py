"""Deterministic source-gender fidelity for model-authored Memory candidates.

Issue #662 records the reaction model writing a third-person gendered pronoun
("他"/"她") into ``memory_candidates`` even though the source event carried no
gender information at all, and that candidate then became durable Memory. The
prompt already states the source-fidelity rule (``_PERSON_SOURCE_FIDELITY`` in
``llm/client.py``); repeating it did not change the output, so the rule is
enforced here, after the model and before the candidate reaches long-term
Memory.

Design choices, all deliberately on the conservative side:

* It only acts when the material the reaction could learn the fact from holds
  **no** gender evidence at all. Detecting evidence is intentionally generous:
  a missed intervention keeps today's behaviour, a false intervention would
  rewrite a legitimate sentence.
* Evidence and replacement use the same pronoun matcher. 他 is also a
  non-pronoun morpheme in neutral compounds such as 其他 and 吉他, and a
  substring match would both treat those as gender evidence (silently turning
  the guard off) and rewrite them into 其TA. Classical "other" uses (他乡,
  他人, 他日) are left alone for the same reason. The Latin neutral pronoun
  TA needs a token boundary so ordinary English words such as data or start
  are not mistaken for evidence.
* It never drops content. An unsupported pronoun is replaced in place with the
  gender-neutral ``TA``, so the referent and the surrounding fact survive; a
  rejected candidate would silently lose something the user really said.
* Evidence is scoped to the memory type. ``source_text`` is what the user
  actually said (current event plus earlier user messages); ``persona_text`` is
  the character's own setting and only counts for memories about the character.
  The character's gender is not the user's gender, and the person's own earlier
  sentence is not a source either, otherwise one guess would license the next.
"""

from __future__ import annotations

import re

NEUTRAL_PRONOUN = "TA"

# Memory types whose subject is the character itself, so the character's own
# setting is a legitimate source for a gendered pronoun.
CHARACTER_SCOPED_MEMORY_TYPES = frozenset({"SELF", "LIFE", "DIARY"})

# 他 is only a personal pronoun outside neutral compounds. "其他"/"吉他" are
# the common ones; the trailing list covers classical "other" uses such as
# 他人 / 他乡 / 他日 / 他杀. Without these boundaries the guard would both
# corrupt a neutral candidate and treat the most ordinary Chinese sentence as
# proof of gender.
_THIRD_PERSON_PRONOUN = re.compile(
    r"(?<!其)(?<!吉)他(?!(?:人|者|乡|日|处|方|杀|律|国|物|年|地))|[她祂]"
)

# Any one of these in the evidence text means gender was actually available.
_EXPLICIT_GENDER = re.compile(
    r"男|女|性别|先生|女士|小姐|姑娘|小哥|男性|女性|男孩|女孩|男生|女生"
)

# Kinship words carry grammatical gender in Chinese, but a few compounds are
# neutral: 师父, 弟子, 字母/分母/声母/韵母/母语, 螺母, 酵母, 航母.
_KINSHIP_GENDER = re.compile(
    r"爸|妈|(?<!师)父|(?<!字)(?<!分)(?<!声)(?<!韵)(?<!螺)(?<!酵)(?<!航)(?<!空)母(?!语)|"
    r"哥|姐|(?<!子)弟(?!子)|妹|"
    r"老公|老婆|丈夫|妻子|儿子|女儿|男友|女友|男朋友|女朋友"
)

_ENGLISH_GENDER = re.compile(r"\b(?:boy|girl|male|female)\b", re.IGNORECASE)
# The gender-neutral pronoun written as Latin letters must be a whole token;
# "data"/"start"/"table" contain "ta" but say nothing about gender.
_LATIN_NEUTRAL_PRONOUN = re.compile(r"(?<![A-Za-z])[Tt][Aa](?![A-Za-z])")

_GENDER_EVIDENCE = (
    _THIRD_PERSON_PRONOUN,
    _EXPLICIT_GENDER,
    _KINSHIP_GENDER,
    _ENGLISH_GENDER,
    _LATIN_NEUTRAL_PRONOUN,
)


def has_gender_evidence(*texts: str) -> bool:
    """True when any supplied text actually contains gender information."""

    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        if any(pattern.search(text) for pattern in _GENDER_EVIDENCE):
            return True
    return False


def enforce_source_gender_fidelity(
    content: str,
    memory_type: str = "EPISODIC",
    *,
    source_text: str = "",
    persona_text: str = "",
) -> tuple[str, int]:
    """Neutralize gendered pronouns the source never supported.

    Returns the (possibly rewritten) content and how many pronouns were
    replaced. A replacement count of 0 means the content is unchanged.
    """

    if not content or not _THIRD_PERSON_PRONOUN.search(content):
        return content, 0

    evidence = [source_text or ""]
    if str(memory_type or "").strip().upper() in CHARACTER_SCOPED_MEMORY_TYPES:
        evidence.append(persona_text or "")
    if has_gender_evidence(*evidence):
        return content, 0

    neutralized, replaced = _THIRD_PERSON_PRONOUN.subn(NEUTRAL_PRONOUN, content)
    return neutralized, replaced
