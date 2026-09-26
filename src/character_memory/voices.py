"""Voice registration: templates carry the clip, characters name one.

Two trees, not one.

A **template** under ``voices/<name>.yaml`` is the only carrier of a voice. It
holds ``ref_audio`` (resolved relative to the template's own directory) and the
``ref_text`` GSV clones from, so nothing else needs to know either.

A **character reference** at ``personas/<id>/voice.yaml`` is a pointer and
nothing more: one line, ``template: <name>``. Keeping the clip out of it is what
lets two characters share one voice, and it is enforced -- any other key in that
file is rejected, the ``ref_audio``/``ref_text`` form included.

Both trees are optional and both fail the same way: a missing file means "not
configured" and degrades silently, a file that exists but cannot be trusted
raises loudly. GSV cannot recover from a missing reference clip or an empty
transcript at synthesis time (``cache_prompt_audio`` raises on empty prompt
text), so a half-configured voice is a load-time error instead of a surprise at
the first request.

Neither tree is committed. A template carries a local reference clip, and a
character reference would dangle without one, so a fresh clone has no configured
voices at all: that is the "not configured" case above, not a broken install.

:func:`discover_templates` and :func:`discover_character_voices` read the two
trees; :func:`resolve_voice_registry` merges them into the single
``{name: profile}`` registry a request is answered from, where a character id
overrides a template of the same name.

One thing this module reads is not a voice document: :func:`_character_id` opens
``persona.yaml`` for its ``id`` field, because that field -- not the directory
name -- is what the browser sends as the requested voice. It duplicates
``config.py``'s ``discover_character_profiles`` expression on purpose, and the
two must stay identical or a character gets keyed under a name nobody asks for.

Mirrors the sticker loader's shape: a missing asset degrades silently, an
invalid one raises loudly.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


logger = logging.getLogger("character_memory.voices")


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


_DocumentT = TypeVar("_DocumentT", bound=BaseModel)


def _read_document(
    path: Path,
    model: type[_DocumentT],
    *,
    unreadable: str,
    empty: str,
    non_mapping: str,
) -> _DocumentT:
    """Read, parse and validate one voice document at ``path``.

    Every reader of this schema fails the same three ways -- unreadable,
    empty, or not a mapping -- and each failure has to arrive as a
    :class:`VoiceProfileError`. The *wording* differs per reader and is a
    cross-module contract, so each caller passes its own clauses rather than
    the helper inventing them: ``unreadable`` is the clause after the path,
    ``empty`` the whole message for a null document, and ``non_mapping`` a
    message template whose ``{kind}`` is filled with the type YAML produced.
    Keeping the words at the call site is what lets this extraction leave
    every existing message byte-identical.

    Whether the file is missing stays with the caller: the three readers
    disagree about it (raise, ``None``, ``None``).
    """

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VoiceProfileError(f"{path}: {unreadable}: {exc}") from exc

    if raw is None:
        raise VoiceProfileError(f"{path}: {empty}")
    if not isinstance(raw, dict):
        raise VoiceProfileError(f"{path}: {non_mapping.format(kind=type(raw).__name__)}")

    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise VoiceProfileError(
            f"{path}: invalid voice profile: {_describe_validation_error(exc)}"
        ) from exc


TEMPLATE_FILE_SUFFIX = ".yaml"
CHARACTER_VOICE_FILENAME = "voice.yaml"


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

    document = _read_document(
        path,
        _VoiceDocument,
        unreadable="unreadable template",
        empty="empty template; ref_audio and ref_text are required",
        non_mapping="expected a YAML mapping, got {kind}",
    )

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
    voice_path = persona_path.parent / CHARACTER_VOICE_FILENAME
    if not voice_path.is_file():
        return None

    document = _read_document(
        voice_path,
        _VoiceDocument,
        unreadable="unreadable voice profile",
        empty="empty voice profile; ref_audio and ref_text are required",
        non_mapping="expected a YAML mapping, got {kind}",
    )

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


class _CharacterVoiceDocument(BaseModel):
    """Raw shape of a character's ``voice.yaml``: a reference and nothing else.

    ``extra="forbid"`` is load-bearing twice over. It keeps the old
    self-contained form (``ref_audio``/``ref_text``) from silently working,
    which would let a character hold a clip again; and it keeps ``gpt_model``
    out, because inheriting a base model is a template-level decision -- the
    same clip under two characters must not resolve to two different models.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    template: str


def load_character_voice(
    persona_path: str | Path, *, voices_root: str | Path | None = None
) -> str | None:
    """Return the template a character references, or ``None`` when it has none.

    ``None`` is the normal case and means "this character was never given a
    voice"; the caller degrades to the default template. A file that exists but
    cannot be trusted raises: the user configured this character deliberately,
    and silently falling back would make a wrong voice hard to notice.

    ``voices_root`` is the caller's own template directory and is used *only* to
    name the fix in the pre-template error. A reader that says "put a template
    under voices" while its caller reads somewhere else sends the operator to a
    directory nothing will ever look at, so the caller passes what it reads.
    """

    persona_path = Path(persona_path)
    voice_path = persona_path.parent / CHARACTER_VOICE_FILENAME
    if not voice_path.is_file():
        return None

    try:
        document = _read_document(
            voice_path,
            _CharacterVoiceDocument,
            unreadable="unreadable voice reference",
            # A null document and a non-mapping one are the same complaint here:
            # this file has exactly one thing to say, and neither says it.
            empty="expected a YAML mapping with a 'template' key",
            non_mapping="expected a YAML mapping with a 'template' key",
        )
    except VoiceProfileError as exc:
        # The pre-template form carried ``ref_audio``/``ref_text`` inline. This
        # model declares neither and forbids extras, so either word appearing in
        # this message means the file is in that form -- which is *not* read any
        # more, and nothing converts it. The fix is to build the clip into a
        # template, and that is worth spelling out: the validation message on its
        # own names the file but not the way out of it.
        if "ref_audio" in str(exc) or "ref_text" in str(exc):
            raise VoiceProfileError(
                f"{exc}; this is the pre-template form, which is no longer read -- "
                f"move the clip into a template under {template_root(voices_root)} and leave "
                f"only 'template: <name>' in this file"
            ) from exc
        raise

    name = (document.template or "").strip()
    if not name:
        raise VoiceProfileError(f"{voice_path}: template is empty")
    return name


def _character_id(persona_path: Path) -> str:
    """Resolve a character's id exactly as ``discover_character_profiles`` does.

    The ``id`` field wins and the directory name is the fallback. It has to be
    the *same expression* as ``config.py:180``, because the browser asks for a
    voice by ``profile["id"]`` -- that field, not the directory. A registry
    anchored on the directory name answers a question nobody asks for any
    persona whose two disagree, and the character drops to the default template
    without a word.

    An unreadable persona document degrades to the directory name rather than
    raising: whether a persona is usable is the persona loader's call to make
    (``discover_character_profiles`` drops it), and this function has no
    standing to fail the whole voice registry over it.
    """

    try:
        data = yaml.safe_load(persona_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        data = {}
    return str(data.get("id") or persona_path.parent.name).strip()


def discover_character_voices(
    persona_paths: Iterable[str | Path],
    *,
    voices_root: str | Path | None = None,
) -> dict[str, str]:
    """Build ``{character_id: template_name}`` for every persona that names one.

    ``persona_paths`` are ``*/persona.yaml`` paths, the same set the app
    discovers. The key is the character's id per :func:`_character_id`, which is
    what the browser sends; a character with no ``voice.yaml`` is simply absent,
    and one whose ``voice.yaml`` is unusable raises from
    :func:`load_character_voice` before this function has an id to key on.
    ``voices_root`` is passed straight through to that reader.

    The line between raising and skipping is *discoverability*: raise when a
    character the app will show could get the wrong voice (the duplicate id
    below), skip when the app will not show it at all (a blank id).
    """

    references: dict[str, str] = {}
    for persona_path in persona_paths:
        persona_path = Path(persona_path)
        name = load_character_voice(persona_path, voices_root=voices_root)
        if name is None:
            continue
        character_id = _character_id(persona_path)
        if not character_id:
            # Reachable only when the ``id`` field is present but blank; the
            # directory name is the fallback otherwise. ``config.py:181`` drops
            # such a persona, so it is not discoverable and no request can name
            # it -- there is no voice to get wrong, which is what earns a skip
            # where the duplicate-id case below earns a raise. The sidecar also
            # must not refuse to start over a character nobody can select.
            logger.warning(
                "ignoring %s: persona id is empty and the character is not discoverable",
                persona_path.parent / CHARACTER_VOICE_FILENAME,
            )
            continue
        previous = references.get(character_id)
        if previous is not None:
            raise VoiceProfileError(
                f"duplicate character id {character_id!r}: two voice files claim it"
            )
        references[character_id] = name
    return references


def resolve_voice_registry(
    *,
    templates: dict[str, VoiceProfile],
    character_voices: dict[str, str],
) -> dict[str, VoiceProfile]:
    """Merge the template tree and the character tree into one registry.

    The result maps both template names and character ids onto profiles, so a
    request for either resolves in one lookup. A character overrides a template
    of the same name, because the character is the more specific answer.

    A character pointing at a template that does not exist raises: that is a
    configuration error, not an unconfigured character. An *unreferenced* missing
    template is not this function's business -- readiness (``_asset_status``)
    reports a default template that was never created.
    """

    registry: dict[str, VoiceProfile] = dict(templates)

    for character_id, template_name in character_voices.items():
        profile = templates.get(template_name)
        if profile is None:
            raise VoiceProfileError(
                f"{character_id} references unknown template {template_name!r}"
            )
        # Re-key onto the character id so the browser's ``voice: <character id>``
        # resolves directly. Copied, not shared: ``registry[k].voice_id == k``
        # then holds for every key, and two characters on one template cannot
        # alias a single mutable object.
        registry[character_id] = profile.model_copy(update={"voice_id": character_id})

    return registry
