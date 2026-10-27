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

_THIRD_PERSON_PRONOUN = re.compile(r"[他她祂]")

# Any one of these in the evidence text means gender was actually available.
# Collected as substrings on purpose: "堂哥", "妈妈", "她是" all count.
_GENDER_EVIDENCE_MARKERS = (
    # third-person pronouns themselves: if the source used one, the model may
    # have echoed a referent that was really there
    "他", "她", "祂", "TA", "ta", "Ta",
    # explicit statements
    "男", "女", "性别", "先生", "女士", "小姐", "姑娘", "小哥",
    "男性", "女性", "男孩", "女孩", "男生", "女生",
    # kinship and relationship words, which carry grammatical gender in Chinese
    "爸", "妈", "父", "母", "哥", "姐", "弟", "妹",
    "老公", "老婆", "丈夫", "妻子", "儿子", "女儿",
    "男友", "女友", "男朋友", "女朋友",
    "boy", "girl", "male", "female",
)


def has_gender_evidence(*texts: str) -> bool:
    """True when any supplied text actually contains gender information."""

    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        if any(marker in text for marker in _GENDER_EVIDENCE_MARKERS):
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
