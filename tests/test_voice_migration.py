"""One-shot migrations onto the template model.

Both migrations run before any strict reader sees the tree: migration B reads
the old self-contained character files, which the new reader rejects by design.
Running validation first would fail on the very files this needs to move --
``test_the_strict_reader_rejects_the_pre_migration_file`` pins that, because it
is the only reason this module runs ahead of validation instead of behind it.

Both are **per item**: each character and each template decides for itself
whether it is already on disk. A single "is ``voices/`` empty" gate would strand
every remaining character the moment any template existed, and the strict reader
rejects the format those stranded characters are still in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from character_memory.voice_migration import migrate_voices
from character_memory.voices import (
    VoiceProfileError,
    discover_character_voices,
    discover_templates,
    load_character_voice,
    resolve_voice_registry,
)


def _legacy_character(root: Path, character_id: str) -> Path:
    """Write the old form: audio beside the persona, referenced relatively."""

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    audio = persona.parent / "voice" / "abcdef0123456789.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF-old")
    (persona.parent / "voice.yaml").write_text(
        yaml.safe_dump(
            {
                "voice_id": character_id,
                "ref_audio": "voice/abcdef0123456789.wav",
                "ref_text": "你好，这是试听。",
                "created_at": "2026-09-20T11:26:53+00:00",
                "instruct": "可爱萝莉音",
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return persona


def test_migrates_a_legacy_character_into_a_template_and_a_reference(tmp_path):
    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    persona = _legacy_character(personas, "haru")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.migrated_characters == ["haru"]
    assert report.created_templates == ["haru"]
    assert report.notes == []

    template = yaml.safe_load((voices / "haru.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "你好，这是试听。"
    assert template["instruct"] == "可爱萝莉音"

    # The WAV is copied, not moved: migration stays reversible.
    assert (voices / "haru" / "abcdef0123456789.wav").read_bytes() == b"RIFF-old"
    assert (personas / "haru" / "voice" / "abcdef0123456789.wav").exists()

    # ...and the character is now a reference.
    reference = yaml.safe_load((personas / "haru" / "voice.yaml").read_text(encoding="utf-8"))
    assert reference == {"template": "haru"}

    # Nothing wrote a file the strict readers cannot read back. This is the
    # cross-boundary round trip the migration owes the rest of the system: two
    # modules, two trees, and the schema drift between them shipped silently once.
    assert load_character_voice(persona) == "haru"
    registry = resolve_voice_registry(
        templates=discover_templates(voices),
        character_voices=discover_character_voices([persona]),
    )
    assert registry["haru"].voice_id == "haru"
    assert registry["haru"].ref_text == "你好，这是试听。"
    # The writer emits a *relative* ref_audio; only the reader's resolution
    # branch makes it a real file again.
    assert Path(registry["haru"].ref_audio).is_file()


def test_the_strict_reader_rejects_the_pre_migration_file(tmp_path):
    """Why this runs ahead of validation instead of behind it.

    The reader that has to run *after* migration is the character one:
    ``_CharacterVoiceDocument`` is ``extra="forbid"`` with ``template`` required,
    so it refuses the old self-contained form outright. If validation ran first,
    the sidecar would fail on the very files this module exists to move -- a
    start-up failure whose message points at the wrong thing.

    ``load_voice_profile`` is deliberately *not* the reader tested here: it is
    the legacy reader this migration reads *with*, so it must keep accepting the
    old form. Testing it would assert the opposite of what is true.
    """

    persona = _legacy_character(tmp_path / "personas", "haru")

    with pytest.raises(VoiceProfileError, match="invalid voice profile"):
        load_character_voice(persona)


def test_migrates_the_legacy_env_into_a_template(tmp_path):
    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(audio),
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
        default_template="character-default",
    )

    assert report.created_templates == ["murasame"]
    # The clip is copied in and referenced relatively, the same shape the
    # character migration writes, so the template owns its audio and survives the
    # legacy path being moved or cleaned up.
    assert (voices / "murasame" / "ref.wav").read_bytes() == b"RIFF-env"
    assert audio.exists()
    template = yaml.safe_load((voices / "murasame.yaml").read_text(encoding="utf-8"))
    assert template["ref_audio"] == "murasame/ref.wav"
    assert template["ref_text"] == "参考文本"
    assert template["gpt_model"] is None      # inherits the shared base model

    # Round trip through the reader, not just the raw YAML: a relative ref only
    # becomes a real file again via the reader's resolution branch, which resolves
    # it against the *template's* directory.
    assert set(discover_templates(voices)) == {"murasame"}
    assert Path(discover_templates(voices)["murasame"].ref_audio).is_file()


def test_a_relative_legacy_env_reference_still_resolves_after_migration(tmp_path, monkeypatch):
    """The env pair has really been configured with a relative path.

    The 2026-09-19 spike runbook writes ``.pytest-tmp/gsv-spike/qwen3_ref.wav``,
    so this is not hypothetical. The trap is that the two sides resolve a
    relative path against different bases: the migration checks ``is_file()``
    from the cwd, the reader resolves against the *template's* directory. Record
    the env string verbatim and the result is a template the reader rejects --
    and the repair never comes, because the next run finds the target present and
    refuses to rewrite it.
    """

    monkeypatch.chdir(tmp_path)
    voices = tmp_path / "voices"
    (tmp_path / "spike").mkdir()
    (tmp_path / "spike" / "qwen3_ref.wav").write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": "spike/qwen3_ref.wav",
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
        default_template="murasame",
    )

    assert report.created_templates == ["murasame"]
    # ``discover_templates`` is the call the sidecar's startup makes; it raises if
    # any template is unusable, so this is the start-up path, not a proxy for it.
    assert Path(discover_templates(voices)["murasame"].ref_audio).is_file()


def test_the_legacy_env_adopts_the_default_template_name_when_unnamed(tmp_path):
    """An unnamed legacy pair belonged to whatever ``default_voice`` resolved to.

    Inventing a name here (``"default"``) creates a template nothing ever
    resolves to, while the *real* default template stays missing -- permanently
    "not ready" in Settings Center with a voice configured that no request can
    reach. The env pair is the default voice's own reference clip, so it has to
    land under the default voice's name.
    """

    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": str(audio), "GSV_TTS_REF_TEXT": "参考文本"},
        default_template="murasame",
    )

    assert report.created_templates == ["murasame"]
    assert (voices / "murasame.yaml").is_file()
    assert not (voices / "default.yaml").exists()


def test_legacy_env_without_a_ref_text_is_not_migrated(tmp_path):
    """Half-configured env is not migratable; it must not create a broken template."""

    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": str(audio), "GSV_TTS_REF_TEXT": "  "},
        default_template="murasame",
    )

    assert report.created_templates == []
    assert not voices.exists()


def test_a_character_already_on_the_template_model_is_left_alone(tmp_path):
    """The steady state on every start after the first, and it has to be silent.

    A note per character per start is noise, not information. The file must also
    come out byte-identical: a migration that rewrites its own output on every
    boot is one interrupted write away from an empty ``voice.yaml``.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )
    before = (personas / "haru" / "voice.yaml").read_bytes()

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.created_templates == []
    assert report.migrated_characters == []
    assert report.notes == []
    assert (personas / "haru" / "voice.yaml").read_bytes() == before


def test_a_handmade_template_is_never_overwritten(tmp_path):
    """A template already on disk wins, even when a character is asking for it.

    The character still has to become a reference in the same pass: leaving it
    in the old format is precisely the stranded state the strict reader refuses,
    so "skip the whole character" would recreate the failure this test exists for.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    voices.mkdir(parents=True)
    (voices / "haru.yaml").write_text("ref_text: handwritten\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert (voices / "haru.yaml").read_text(encoding="utf-8") == "ref_text: handwritten\n"
    assert report.created_templates == []
    assert report.migrated_characters == ["haru"]
    assert any("already exists" in note for note in report.notes)
    assert yaml.safe_load((personas / "haru" / "voice.yaml").read_text(encoding="utf-8")) == {
        "template": "haru"
    }
    # A template that was left as it is had no audio copied for it either.
    assert not (voices / "haru").exists()


def test_a_colliding_env_name_does_not_overwrite_the_character_template(tmp_path):
    """``GSV_TTS_VOICE`` names a character that just migrated.

    The character's own clip is the more specific fact; the env pair is a
    leftover from before templates existed. Silently replacing the template would
    move every other character on it onto the legacy voice as well.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    env_audio = tmp_path / "env.wav"
    env_audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(env_audio),
            "GSV_TTS_REF_TEXT": "env 文本",
            "GSV_TTS_VOICE": "haru",
        },
        default_template="murasame",
    )

    template = yaml.safe_load((voices / "haru.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "你好，这是试听。"
    assert report.created_templates == ["haru"]
    assert any("already exists" in note for note in report.notes)


def test_a_failed_env_template_write_is_a_note_rather_than_a_crash(tmp_path):
    """The env side gets the same isolation the character side has.

    A blocked write here used to travel out of ``migrate_voices`` into the
    sidecar's constructor, i.e. no sidecar at all -- the exact failure mode this
    module exists to remove.
    """

    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")
    voices.mkdir(parents=True)
    (voices / "murasame").write_text("not a directory\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(audio),
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
        default_template="murasame",
    )

    assert report.created_templates == []
    assert any("murasame" in note and "failed" in note for note in report.notes)


def test_an_unsafe_voice_id_is_noted_and_the_character_keeps_its_old_file(tmp_path):
    """``voice_id`` comes from a file and becomes a path, so it is checked.

    ``../escape`` would write outside ``voices/``. Refused with a note, and the
    character's ``voice.yaml`` stays in the old form: turning it into a reference
    to a template that was never written would be the stranded state this module
    runs ahead of validation to avoid.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    persona = _legacy_character(personas, "haru")
    voice_file = persona.parent / "voice.yaml"
    document = yaml.safe_load(voice_file.read_text(encoding="utf-8"))
    document["voice_id"] = "../escape"
    voice_file.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    before = voice_file.read_bytes()

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.created_templates == []
    assert report.migrated_characters == []
    assert any("not usable as a file name" in note for note in report.notes)
    assert voice_file.read_bytes() == before
    assert not (tmp_path / "escape.yaml").exists()
    assert not (voices / "escape.yaml").exists()


def test_a_failed_write_does_not_strand_the_other_characters(tmp_path):
    """An unwritable template is one character's problem, not the whole tree's.

    The failure is injected where it really happens rather than mocked: the copy
    step creates ``voices/<voice_id>/``, and a *file* sitting at that path makes
    the ``mkdir`` raise -- the same ``OSError`` class as a locked, read-only or
    full disk.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "a-haru")
    _legacy_character(personas, "b-momo")
    voices.mkdir(parents=True)
    (voices / "a-haru").write_text("not a directory\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.created_templates == ["b-momo"]
    assert report.migrated_characters == ["b-momo"]
    assert any("a-haru" in note and "failed" in note for note in report.notes)
    # Nothing was written for a-haru, so it keeps the old file instead of a
    # reference to a template that is not on disk.
    assert "template" not in yaml.safe_load(
        (personas / "a-haru" / "voice.yaml").read_text(encoding="utf-8")
    )


def test_a_failed_reference_rewrite_still_reports_the_template_it_wrote(tmp_path):
    """The template write and the reference write are separate steps for this case.

    A template that landed has to appear in the report even when the reference
    rewrite then failed, or a caller counting ``created_templates`` disagrees with
    the disk. The blocker is real: a directory squatting on the temporary name the
    atomic write uses, which is what a crashed editor session leaves behind.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    persona = _legacy_character(personas, "a-haru")
    (persona.parent / "voice.yaml.tmp").mkdir()
    _legacy_character(personas, "b-momo")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.created_templates == ["a-haru", "b-momo"]
    assert report.migrated_characters == ["b-momo"]
    assert any("a-haru" in note and "failed" in note for note in report.notes)
    assert (voices / "a-haru" / "abcdef0123456789.wav").is_file()
    assert "template" not in yaml.safe_load(
        (personas / "a-haru" / "voice.yaml").read_text(encoding="utf-8")
    )


def test_a_character_without_a_voice_file_is_silent(tmp_path):
    """Most of a real persona tree has no ``voice.yaml`` at all.

    A missing file must not reach the unreadable-file handler: ``read_text``
    raises ``FileNotFoundError``, which *is* an ``OSError``, so without an
    explicit ``is_file`` guard every voiceless character warns on every start --
    and the one warning that matters gets lost in that noise.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    voiceless = personas / "silent" / "persona.yaml"
    voiceless.parent.mkdir(parents=True)
    voiceless.write_text("id: silent\nname: silent\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.notes == []
    assert report.migrated_characters == ["haru"]


def test_a_malformed_character_does_not_stop_the_others(tmp_path):
    """One unreadable ``voice.yaml`` must not strand every character after it."""

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    broken = _legacy_character(personas, "a-broken")
    (broken.parent / "voice.yaml").write_text("- not a mapping\n", encoding="utf-8")
    _legacy_character(personas, "b-good")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.migrated_characters == ["b-good"]
    assert report.created_templates == ["b-good"]
    assert any("a-broken" in note for note in report.notes)


def test_a_missing_reference_clip_is_noted_and_skipped(tmp_path):
    """A persona pointing at a clip that is gone cannot become a template.

    A note rather than a raise: the file is on disk, it is simply not usable,
    and one such character is not a reason to abort the upgrade for the rest.
    """

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    persona = _legacy_character(personas, "haru")
    (persona.parent / "voice" / "abcdef0123456789.wav").unlink()

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={},
        default_template="murasame",
    )

    assert report.created_templates == []
    assert report.migrated_characters == []
    assert any("haru" in note and "ref_audio not found" in note for note in report.notes)
    # Nothing was created for this character, so it has nothing to point at and
    # keeps its old file -- rather than a reference to a template that is not there.
    assert "template" not in (
        yaml.safe_load((persona.parent / "voice.yaml").read_text(encoding="utf-8")) or {}
    )


def test_both_migrations_run_together_without_colliding(tmp_path):
    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF-env")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={
            "GSV_TTS_REF_AUDIO": str(audio),
            "GSV_TTS_REF_TEXT": "参考文本",
            "GSV_TTS_VOICE": "murasame",
        },
        default_template="character-default",
    )

    assert sorted(report.created_templates) == ["haru", "murasame"]
    assert report.migrated_characters == ["haru"]
    assert report.notes == []
