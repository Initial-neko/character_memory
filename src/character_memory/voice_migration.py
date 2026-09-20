"""One-shot migrations onto the template model.

Two independent moves, both triggered the same way and both run at most once:

A. The legacy ``GSV_TTS_REF_AUDIO``/``GSV_TTS_REF_TEXT`` env pair becomes a
   template, so the settings page can drop those two fields.
B. A character that still holds its own reference clip becomes a template plus
   a one-line reference.

**Ordering is a hard constraint.** B reads the old self-contained character
files, which the new strict reader rejects (``extra="forbid"``). Running
validation before migration would fail on the very files this module exists to
move, so callers must migrate first and validate second.

Nothing here deletes anything. The legacy env keys and the old WAVs stay where
they are: the migration only adds files, which keeps it reversible and keeps it
from touching user configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil

import yaml

from character_memory.voices import (
    VoiceProfileError,
    load_voice_profile,
    template_root,
)


@dataclass
class MigrationReport:
    created_templates: list[str] = field(default_factory=list)
    migrated_characters: list[str] = field(default_factory=list)
    skipped: bool = False
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
    that empties it is not reversible.
    """

    target_dir = voices_root / name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if not target.exists():
        shutil.copy2(source, target)
    return source.name


def _migrate_character_clips(personas_root: Path, voices_root: Path, report: MigrationReport) -> None:
    paths = sorted(personas_root.glob("*/persona.yaml")) if personas_root.exists() else []
    for persona_path in paths:
        character_id = persona_path.parent.name
        try:
            profile = load_voice_profile(persona_path)
        except VoiceProfileError as exc:
            # Already a reference, or unusable. Either way this migration has
            # nothing to move; the strict reader gets to report it later.
            report.notes.append(f"{character_id}: skipped ({exc})")
            continue
        if profile is None:
            continue

        # Read the raw document too. VoiceProfile does not carry the provenance
        # fields, and they cannot be reconstructed -- the VoiceDesign run that
        # produced this clip is gone. The drawer shows them (spec 11), so losing
        # them here would be a silent, permanent loss.
        try:
            raw = yaml.safe_load((persona_path.parent / "voice.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        audio_source = Path(profile.ref_audio)
        filename = _copy_audio(audio_source, voices_root, profile.voice_id)
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

        reference = persona_path.parent / "voice.yaml"
        temp = reference.with_suffix(".yaml.tmp")
        temp.write_text(
            yaml.safe_dump({"template": profile.voice_id}, sort_keys=False), encoding="utf-8"
        )
        temp.replace(reference)

        report.created_templates.append(profile.voice_id)
        report.migrated_characters.append(character_id)


def _migrate_legacy_env(voices_root: Path, legacy_env: dict[str, str], report: MigrationReport) -> None:
    ref_audio = str(legacy_env.get("GSV_TTS_REF_AUDIO") or "").strip()
    ref_text = str(legacy_env.get("GSV_TTS_REF_TEXT") or "").strip()
    if not ref_audio or not ref_text:
        # Half-configured env is not migratable. Creating a template from it
        # would turn "not configured yet" into "configured but broken".
        return
    if not Path(ref_audio).is_file():
        report.notes.append(f"legacy reference audio not found: {ref_audio}")
        return

    name = str(legacy_env.get("GSV_TTS_VOICE") or "").strip() or "default"
    _write_template(
        voices_root,
        name,
        {
            "voice_id": name,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "gpt_model": None,      # inherits the shared base model
            "sovits_model": None,
        },
    )
    report.created_templates.append(name)


def migrate_voices(
    *,
    personas_root: str | Path,
    voices_root: str | Path | None,
    legacy_env: dict[str, str],
) -> MigrationReport:
    """Run both migrations if the template tree is still empty.

    A non-empty ``voices/`` means a previous run already happened (or the user
    built templates by hand); either way the migrations are done and must not
    overwrite anything.
    """

    personas_root = Path(personas_root)
    voices_root = template_root(voices_root)
    report = MigrationReport()

    if voices_root.exists() and any(voices_root.glob("*.yaml")):
        report.skipped = True
        return report

    _migrate_character_clips(personas_root, voices_root, report)
    _migrate_legacy_env(voices_root, legacy_env, report)
    return report
