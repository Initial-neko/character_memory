"""One-shot migrations onto the template model.

Both migrations run before any strict reader sees the tree: migration B reads
the old self-contained character files, which the new reader rejects by design.
Running validation first would fail on the very files this needs to move.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from character_memory.voice_migration import migrate_voices


def _character(root: Path, character_id: str, document: dict) -> Path:
    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return persona


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
    _legacy_character(personas, "haru")

    report = migrate_voices(personas_root=personas, voices_root=voices, legacy_env={})

    assert report.migrated_characters == ["haru"]
    assert report.created_templates == ["haru"]

    template = yaml.safe_load((voices / "haru.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "你好，这是试听。"
    assert template["instruct"] == "可爱萝莉音"

    # The WAV is copied, not moved: migration stays reversible.
    assert (voices / "haru" / "abcdef0123456789.wav").read_bytes() == b"RIFF-old"
    assert (personas / "haru" / "voice" / "abcdef0123456789.wav").exists()

    # ...and the character is now a reference.
    reference = yaml.safe_load((personas / "haru" / "voice.yaml").read_text(encoding="utf-8"))
    assert reference == {"template": "haru"}


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
    )

    assert report.created_templates == ["murasame"]
    template = yaml.safe_load((voices / "murasame.yaml").read_text(encoding="utf-8"))
    assert template["ref_audio"] == str(audio)
    assert template["ref_text"] == "参考文本"
    assert template["gpt_model"] is None      # inherits the shared base model


def test_legacy_env_without_a_ref_text_is_not_migrated(tmp_path):
    """Half-configured env is not migratable; it must not create a broken template."""

    voices = tmp_path / "voices"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"RIFF")

    report = migrate_voices(
        personas_root=tmp_path / "personas",
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": str(audio), "GSV_TTS_REF_TEXT": "  "},
    )

    assert report.created_templates == []
    assert not voices.exists()


def test_a_non_empty_voices_directory_skips_both_migrations(tmp_path):
    """Migrations run once. A later run must never overwrite what the user made."""

    personas = tmp_path / "personas"
    voices = tmp_path / "voices"
    _legacy_character(personas, "haru")
    voices.mkdir(parents=True)
    (voices / "handmade.yaml").write_text("ref_text: x\n", encoding="utf-8")

    report = migrate_voices(
        personas_root=personas,
        voices_root=voices,
        legacy_env={"GSV_TTS_REF_AUDIO": "a.wav", "GSV_TTS_REF_TEXT": "t"},
    )

    assert report.skipped is True
    assert report.created_templates == []
    assert report.migrated_characters == []


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
    )

    assert sorted(report.created_templates) == ["haru", "murasame"]
    assert report.migrated_characters == ["haru"]
