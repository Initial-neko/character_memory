"""One-shot migrations onto the template model.

Two independent moves:

A. The legacy ``GSV_TTS_REF_AUDIO``/``GSV_TTS_REF_TEXT`` env pair becomes a
   template, so the settings page can drop those two fields.
B. A character that still holds its own reference clip becomes a template plus
   a one-line reference.

**Ordering is a hard constraint.** B reads the old self-contained character
files, which the new strict reader rejects (``extra="forbid"``). Running
validation before migration would fail on the very files this module exists to
move, so callers must migrate first and validate second.

**Per item, not "while ``voices/`` is empty".** Each character and each template
decides for itself whether it is already on disk, which is what lets an
interrupted run -- or a tree someone built templates into by hand -- finish on
the next start. Nothing already on disk is overwritten, and every refusal is
recorded in :attr:`MigrationReport.notes` rather than raised: one unusable
character must not strand all the others.

What it does *not* do: delete anything. The legacy env keys, the old WAVs and
the original per-persona audio directory all stay where they are, which is what
keeps this reversible. The one file it does rewrite is the character's own
``voice.yaml`` -- it has to, since the old self-contained form is precisely what
the strict reader refuses to load. That rewrite is the migration's whole point,
not a side effect. Every write here -- template, copied audio, reference -- goes
through a temporary name and a replace, so none of them can land half-written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil

import yaml

from character_memory.voices import (
    CHARACTER_VOICE_FILENAME,
    VoiceProfileError,
    load_voice_profile,
    template_root,
)


@dataclass
class MigrationReport:
    created_templates: list[str] = field(default_factory=list)
    migrated_characters: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _write_template(voices_root: Path, name: str, document: dict) -> None:
    """Atomic template write, mirroring ``persona_builder.save_persona``."""

    voices_root.mkdir(parents=True, exist_ok=True)
    target = voices_root / f"{name}.yaml"
    temp = target.with_suffix(".yaml.tmp")
    temp.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temp.replace(target)


def _copy_audio(source: Path, voices_root: Path, name: str) -> str:
    """Copy a clip into the template's own directory and return the bare filename.

    Copied rather than moved: the persona tree is the user's, and a migration
    that empties it is not reversible. Written under a temporary name and
    replaced, like the two YAML writers: a copy killed halfway would otherwise
    leave a truncated WAV that every later run skips (the file exists) and the
    reader accepts (it only asks ``is_file``), so the voice would clone from
    broken audio for good.
    """

    target_dir = voices_root / name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if target.exists():
        return source.name
    temp = target.with_suffix(".wav.tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
    return source.name


def _write_reference(voice_path: Path, name: str) -> None:
    """Rewrite a character's ``voice.yaml`` as a one-line reference, atomically."""

    temp = voice_path.with_suffix(".yaml.tmp")
    temp.write_text(yaml.safe_dump({"template": name}, sort_keys=False), encoding="utf-8")
    temp.replace(voice_path)


def _is_safe_name(name: str) -> bool:
    """Whether a user-supplied voice id may become a file and a directory name.

    ``voice_id`` comes from a file and the legacy ``GSV_TTS_VOICE`` from the
    environment; both are arbitrary strings. ``a/b`` would be written into a
    directory that does not exist, ``../x`` outside ``voices/``. One refused
    name is a note, not a crash.
    """

    return bool(name) and Path(name).name == name and name not in {".", ".."}


def _migrate_character_clips(personas_root: Path, voices_root: Path, report: MigrationReport) -> None:
    paths = sorted(personas_root.glob("*/persona.yaml")) if personas_root.exists() else []
    for persona_path in paths:
        character_id = persona_path.parent.name
        voice_path = persona_path.parent / CHARACTER_VOICE_FILENAME

        if not voice_path.is_file():
            # Never given a voice: the normal case, and the majority of a real
            # persona tree. Guarded *before* the read, because ``read_text`` on a
            # missing file raises ``FileNotFoundError`` -- an ``OSError`` -- and
            # the handler below would then emit an "unreadable voice file" note
            # for every voiceless character on every start.
            continue

        # Read the raw document first: it is both the discriminator below and,
        # for a file that does need migrating, the only source of the provenance
        # fields. ``VoiceProfile`` does not carry them and they cannot be
        # reconstructed -- the VoiceDesign run that produced this clip is gone.
        # The drawer shows them (spec 11), so losing them here would be a silent,
        # permanent loss.
        try:
            raw = yaml.safe_load(voice_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            report.notes.append(f"{character_id}: unreadable voice file ({exc})")
            continue
        if not isinstance(raw, dict):
            raw = {}

        if "template" in raw:
            # Already a reference, so an earlier run did this character. That is
            # the normal state on every start after the first, and one note per
            # character per start would be noise rather than information.
            continue

        try:
            profile = load_voice_profile(persona_path)
        except VoiceProfileError as exc:
            # Neither a reference nor a legacy profile. The strict reader gets to
            # report it; this migration has nothing to move.
            report.notes.append(f"{character_id}: skipped ({exc})")
            continue
        if profile is None:
            continue

        if not _is_safe_name(profile.voice_id):
            report.notes.append(
                f"{character_id}: voice_id {profile.voice_id!r} is not usable as a file name"
            )
            continue

        if (voices_root / f"{profile.voice_id}.yaml").exists():
            # Someone got there first: the user built this template by hand, or an
            # earlier run wrote it and then failed. Never overwrite it -- but the
            # character still has to become a reference, because the old format is
            # exactly what the strict reader refuses to load.
            report.notes.append(
                f"{character_id}: template {profile.voice_id!r} already exists; left as it is"
            )
        else:
            try:
                filename = _copy_audio(Path(profile.ref_audio), voices_root, profile.voice_id)
                _write_template(
                    voices_root,
                    profile.voice_id,
                    {
                        "voice_id": profile.voice_id,
                        "ref_audio": f"{profile.voice_id}/{filename}",
                        "ref_text": profile.ref_text,
                        "gpt_model": profile.gpt_model,
                        "sovits_model": profile.sovits_model,
                        **{key: raw[key] for key in ("created_at", "instruct", "model") if key in raw},
                    },
                )
            except OSError as exc:
                # A locked or unwritable file must not strand every character
                # after it: the rest of the tree still has to be reachable.
                report.notes.append(f"{character_id}: failed ({exc})")
                continue
            # Recorded as soon as the template is on disk, before the reference
            # rewrite below. If that rewrite fails, the template still exists and
            # the report has to agree with the disk.
            report.created_templates.append(profile.voice_id)

        try:
            _write_reference(voice_path, profile.voice_id)
        except OSError as exc:
            # The two writes are separate steps so that this one -- the file a
            # user is most likely to be holding open, since it is the one they
            # would edit -- cannot take the remaining characters down with it.
            report.notes.append(f"{character_id}: failed ({exc})")
            continue

        report.migrated_characters.append(character_id)


def _migrate_legacy_env(
    voices_root: Path,
    legacy_env: dict[str, str],
    report: MigrationReport,
    default_template: str,
) -> None:
    ref_audio = str(legacy_env.get("GSV_TTS_REF_AUDIO") or "").strip()
    ref_text = str(legacy_env.get("GSV_TTS_REF_TEXT") or "").strip()
    if not ref_audio or not ref_text:
        # Half-configured env is not migratable. Creating a template from it
        # would turn "not configured yet" into "configured but broken".
        return
    if not Path(ref_audio).is_file():
        report.notes.append(f"legacy reference audio not found: {ref_audio}")
        return

    # The legacy pair *was* the voice ``GSV_TTS_VOICE`` named, so an unnamed pair
    # belonged to whatever ``default_voice`` resolved to -- not to a name invented
    # here. ``or "default"`` was wrong twice over: it created a template nothing
    # ever resolves to, and it left the *real* default template missing, which is
    # "not ready" on every start.
    name = str(legacy_env.get("GSV_TTS_VOICE") or "").strip() or default_template
    if not _is_safe_name(name):
        report.notes.append(f"legacy voice name {name!r} is not usable as a file name")
        return
    if (voices_root / f"{name}.yaml").exists():
        # Recorded, not silent: the target existing can also mean a character owns
        # the name, in which case the legacy voice was *not* migrated and nothing
        # else would say so. Either way it is never overwritten.
        report.notes.append(f"legacy env template {name!r} already exists; left as it is")
        return

    try:
        # Copied in and referenced relatively, the same shape the character
        # migration writes. Recording the env string as given is a trap: the
        # existence check above resolves a relative path against the *cwd*, while
        # the reader resolves it against the *template's* directory -- so a
        # relative env value would produce a template the reader rejects, and the
        # branch above would then refuse to rewrite it on every later run.
        filename = _copy_audio(Path(ref_audio), voices_root, name)
        _write_template(
            voices_root,
            name,
            {
                "voice_id": name,
                "ref_audio": f"{name}/{filename}",
                "ref_text": ref_text,
                "gpt_model": None,      # inherits the shared base model
                "sovits_model": None,
            },
        )
    except OSError as exc:
        # Same reason as the character side: an unwritable ``voices/`` must not
        # become a sidecar that will not start.
        report.notes.append(f"legacy env template {name!r}: failed ({exc})")
        return
    report.created_templates.append(name)


def migrate_voices(
    *,
    personas_root: str | Path,
    voices_root: str | Path | None,
    legacy_env: dict[str, str],
    default_template: str,
) -> MigrationReport:
    """Run both migrations, one item at a time, each item at most once.

    Item by item, not "only while ``voices/`` is empty". A run that was
    interrupted, or a user who built a template by hand before the first start,
    must not strand the remaining characters in the old format: the strict reader
    rejects that format outright, so a stranded character is not cosmetic -- the
    sidecar will not start, and the message does not say why.

    Nothing already on disk is overwritten, and every skip and refusal is
    recorded in ``notes``, which is the only thing that explains a character that
    came out voiceless. An empty ``created_templates``/``migrated_characters``
    pair means there was nothing left to do.

    ``default_template`` names the template built from the legacy env pair when
    ``GSV_TTS_VOICE`` never named one. It is the caller's ``default_voice``:
    inventing a name here would create a template nothing resolves to.
    """

    personas_root = Path(personas_root)
    voices_root = template_root(voices_root)
    report = MigrationReport()

    _migrate_character_clips(personas_root, voices_root, report)
    _migrate_legacy_env(voices_root, legacy_env, report, default_template)
    return report
