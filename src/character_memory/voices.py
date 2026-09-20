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

import os
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

    A template under ``voices/`` shares this shape; only the *default* for a
    missing ``voice_id`` differs (the file stem instead of a directory name),
    which is each reader's own choice rather than a schema difference.

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


TEMPLATE_FILE_SUFFIX = ".yaml"


def template_root(configured: str | Path | None = None) -> Path:
    """Resolve the templates directory, preferring the argument over the env.

    Mirrors ``gsv_tts_experiment._persona_root``: a relative glob resolves
    against the *cwd*, which the sidecar does not control, so the start script
    and the dev stack pin an absolute path through ``GSV_TTS_VOICES_ROOT``.
    """

    if configured is not None:
        value = str(configured).strip()
        if value:
            return Path(value)
    return Path((os.getenv("GSV_TTS_VOICES_ROOT") or "").strip() or "voices")


def load_template(path: str | Path) -> VoiceProfile:
    """Load a template document.

    Same schema as :class:`VoiceProfile` plus the provenance fields, so the
    reader is shared with the legacy per-character loader. Raises
    :class:`VoiceProfileError` for anything unusable: GSV cannot recover from a
    bad reference clip at synthesis time.

    Sharing :class:`_VoiceDocument` with the per-character loader is deliberate,
    and the freeze route round-trips its template writer through here for the
    same reason the per-character round-trip exists: a reader/writer schema
    drift shipped once already, and it failed silently.
    """

    path = Path(path)
    if not path.is_file():
        raise VoiceProfileError(f"{path}: template not found")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceProfileError(f"{path}: unreadable template: {exc}") from exc

    if raw is None:
        raise VoiceProfileError(f"{path}: empty template; ref_audio and ref_text are required")
    if not isinstance(raw, dict):
        raise VoiceProfileError(f"{path}: expected a YAML mapping, got {type(raw).__name__}")

    try:
        document = _VoiceDocument.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(
            f"{path}: invalid voice profile: {_describe_validation_error(exc)}"
        ) from exc

    if document.voice_id is None:
        # Absent is normal: a template's identity is its filename.
        voice_id = path.stem
    else:
        voice_id = document.voice_id.strip()
    if not voice_id:
        # Explicitly empty is a malformed document, not a missing one -- the
        # same line the per-character loader draws. Two readers sharing
        # ``_VoiceDocument`` must not disagree about one field.
        raise VoiceProfileError(
            f"{path}: voice_id is empty; omit it to default to the template file name"
        )

    raw_audio = (document.ref_audio or "").strip()
    if not raw_audio:
        raise VoiceProfileError(f"{path}: ref_audio is required")
    audio_path = Path(raw_audio)
    if not audio_path.is_absolute():
        audio_path = path.parent / audio_path
    resolved_audio = audio_path.resolve()
    if not resolved_audio.is_file():
        raise VoiceProfileError(f"{path}: ref_audio not found: {resolved_audio}")

    ref_text = (document.ref_text or "").strip()
    if not ref_text:
        raise VoiceProfileError(f"{path}: ref_text is empty; GSV needs a reference transcript")

    return VoiceProfile(
        voice_id=voice_id,
        ref_audio=str(resolved_audio),
        ref_text=ref_text,
        gpt_model=_optional_str(document.gpt_model),
        sovits_model=_optional_str(document.sovits_model),
    )


def discover_templates(root: str | Path | None = None) -> dict[str, VoiceProfile]:
    """Build the ``{voice_id: profile}`` registry for every ``voices/*.yaml``.

    A missing root contributes nothing. Duplicate voice ids are an error: one
    template silently shadowing another is exactly what this registry prevents.
    """

    directory = template_root(root)
    paths = sorted(directory.glob(f"*{TEMPLATE_FILE_SUFFIX}")) if directory.exists() else []

    profiles: dict[str, VoiceProfile] = {}
    sources: dict[str, Path] = {}
    for path in paths:
        profile = load_template(path)
        previous = sources.get(profile.voice_id)
        if previous is not None:
            raise VoiceProfileError(
                f"duplicate voice_id {profile.voice_id!r}: {previous} and {path}"
            )
        profiles[profile.voice_id] = profile
        sources[profile.voice_id] = path
    return profiles


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
