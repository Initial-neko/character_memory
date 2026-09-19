"""Per-persona voice profiles.

``voice.yaml`` sits next to a character's ``persona.yaml`` and points at the
reference audio + transcript GSV needs to clone that character's voice. The
file is optional: a persona without one simply has no registered voice. A
persona that *does* have one is treated as a load-time contract, because GSV
cannot recover from a missing reference clip or an empty transcript at
synthesis time (``cache_prompt_audio`` raises on empty prompt text).

Mirrors the sticker loader's shape: a missing asset degrades silently, an
invalid one raises loudly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class VoiceProfileError(ValueError):
    """Raised when a voice.yaml exists but is invalid."""


class VoiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str
    ref_audio: str
    ref_text: str
    gpt_model: str | None = None
    sovits_model: str | None = None


class _VoiceDocument(BaseModel):
    """Raw shape of voice.yaml; voice_id is optional and defaults to the dir name.

    ``created_at`` / ``instruct`` / ``model`` are provenance, written by the
    freeze route in ``tts_lab.py``. GSV never reads them, but a frozen
    VoiceDesign clip cannot be regenerated from its prompt, so the record is the
    only thing that explains where a clip came from.

    They are *declared* rather than tolerated via ``extra="ignore"`` on purpose:
    the fields above are the synthesis contract, and a typo in one of them
    (``sovits_mdoel``) must stay a loud error instead of silently inheriting the
    global model. ``tests/test_tts_lab_voice_freeze.py`` round-trips the writer
    through this reader so the two schemas cannot drift apart again -- that
    drift shipped once already, and it failed silently.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    voice_id: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    gpt_model: str | None = None
    sovits_model: str | None = None

    # Provenance: written by the freeze route, unread by GSV. ``created_at`` is
    # typed loosely because YAML parses an unquoted timestamp into a datetime,
    # while the writer emits a quoted string; a hand-edited file spells it either way.
    created_at: datetime | str | None = None
    instruct: str | None = None
    model: str | None = None


def _describe_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _optional_str(value: str | None) -> str | None:
    """Empty/whitespace means "not set" so the global config is inherited."""

    if value is None:
        return None
    return value.strip() or None


def load_voice_profile(persona_path: str | Path) -> VoiceProfile | None:
    """Load ``voice.yaml`` beside ``persona_path``.

    Returns ``None`` when the file is absent (the normal case). Raises
    :class:`VoiceProfileError` when it exists but cannot be trusted.
    """

    persona_path = Path(persona_path)
    voice_path = persona_path.parent / "voice.yaml"
    if not voice_path.is_file():
        return None

    try:
        raw = yaml.safe_load(voice_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceProfileError(f"{voice_path}: unreadable voice profile: {exc}") from exc

    if raw is None:
        raise VoiceProfileError(
            f"{voice_path}: empty voice profile; ref_audio and ref_text are required"
        )
    if not isinstance(raw, dict):
        raise VoiceProfileError(
            f"{voice_path}: expected a YAML mapping, got {type(raw).__name__}"
        )

    try:
        document = _VoiceDocument.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(
            f"{voice_path}: invalid voice profile: {_describe_validation_error(exc)}"
        ) from exc

    if document.voice_id is None:
        voice_id = persona_path.parent.name
    else:
        voice_id = document.voice_id.strip()
    if not voice_id:
        raise VoiceProfileError(
            f"{voice_path}: voice_id is empty; omit it to default to the persona directory name"
        )

    raw_audio = (document.ref_audio or "").strip()
    if not raw_audio:
        raise VoiceProfileError(f"{voice_path}: ref_audio is required")
    audio_path = Path(raw_audio)
    if not audio_path.is_absolute():
        audio_path = persona_path.parent / audio_path
    resolved_audio = audio_path.resolve()
    if not resolved_audio.is_file():
        raise VoiceProfileError(f"{voice_path}: ref_audio not found: {resolved_audio}")

    ref_text = (document.ref_text or "").strip()
    if not ref_text:
        raise VoiceProfileError(
            f"{voice_path}: ref_text is empty; GSV needs a reference transcript"
        )

    return VoiceProfile(
        voice_id=voice_id,
        ref_audio=str(resolved_audio),
        ref_text=ref_text,
        gpt_model=_optional_str(document.gpt_model),
        sovits_model=_optional_str(document.sovits_model),
    )


def discover_voice_profiles(
    persona_paths: Iterable[str | Path],
) -> dict[str, VoiceProfile]:
    """Build the ``{voice_id: profile}`` registry for every persona that has a voice.

    Duplicate voice ids are an error: one persona silently shadowing another is
    exactly the failure this registry exists to prevent.
    """

    profiles: dict[str, VoiceProfile] = {}
    sources: dict[str, Path] = {}
    for persona_path in persona_paths:
        profile = load_voice_profile(persona_path)
        if profile is None:
            continue
        previous = sources.get(profile.voice_id)
        if previous is not None:
            raise VoiceProfileError(
                f"duplicate voice_id {profile.voice_id!r}: "
                f"{previous} and {Path(persona_path)}"
            )
        profiles[profile.voice_id] = profile
        sources[profile.voice_id] = Path(persona_path)
    return profiles
