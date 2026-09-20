"""Template-side readers.

A template is a named ``VoiceProfile`` under ``voices/``. It is the only thing
in the repo that owns a reference clip; a character only ever names one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from character_memory.voices import (
    VoiceProfile,
    VoiceProfileError,
    discover_templates,
    load_template,
    template_root,
)


def _write_template(root: Path, name: str, **fields) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    document = {
        "ref_audio": fields.pop("ref_audio", str(audio)),
        "ref_text": fields.pop("ref_text", "你好，今天天气不错。"),
        **fields,
    }
    path = root / f"{name}.yaml"
    import yaml

    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_load_template_reads_a_named_profile(tmp_path):
    _write_template(tmp_path, "haru")

    profile = load_template(tmp_path / "haru.yaml")

    assert isinstance(profile, VoiceProfile)
    assert profile.voice_id == "haru"          # defaults to the file stem
    assert profile.ref_text == "你好，今天天气不错。"


def test_load_template_rejects_ref_audio_that_does_not_exist(tmp_path):
    path = _write_template(tmp_path, "haru", ref_audio=str(tmp_path / "missing.wav"))

    with pytest.raises(VoiceProfileError, match="ref_audio not found"):
        load_template(path)


def test_load_template_rejects_empty_ref_text(tmp_path):
    path = _write_template(tmp_path, "haru", ref_text="   ")

    with pytest.raises(VoiceProfileError, match="ref_text is empty"):
        load_template(path)


def test_load_template_distinguishes_absent_voice_id_from_an_empty_one(tmp_path):
    """Absent means "derive it", explicitly empty is malformed.

    A template's identity is its file stem, so omitting ``voice_id`` is a
    normal, supported shape and must keep loading. Spelling the key out and
    leaving it blank is a different document -- a malformed one -- and has to
    be as loud here as it is in ``load_voice_profile``. Both readers share
    ``_VoiceDocument``; disagreeing about one field is exactly the silent
    reader/writer drift this module has already shipped once.
    """

    empty = _write_template(tmp_path, "blank", voice_id="   ")
    with pytest.raises(VoiceProfileError, match="voice_id is empty"):
        load_template(empty)

    absent = _write_template(tmp_path, "haru")
    profile = load_template(absent)

    assert profile.voice_id == "haru"


def test_load_template_rejects_unknown_fields(tmp_path):
    """A typo must stay loud: sovits_mdoel would otherwise inherit the global model."""

    path = _write_template(tmp_path, "haru", sovits_mdoel="x.pth")

    with pytest.raises(VoiceProfileError, match="invalid voice profile"):
        load_template(path)


def test_discover_templates_is_empty_when_root_is_missing(tmp_path):
    """A missing root must never stop the sidecar from starting."""

    assert discover_templates(tmp_path / "nope") == {}


def test_discover_templates_rejects_duplicate_voice_id(tmp_path):
    _write_template(tmp_path, "haru")
    _write_template(tmp_path, "haru2", voice_id="haru")

    with pytest.raises(VoiceProfileError, match="duplicate voice_id 'haru'"):
        discover_templates(tmp_path)


def test_template_root_prefers_the_explicit_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "from-env"))

    assert template_root(tmp_path / "explicit") == tmp_path / "explicit"
    assert template_root(None) == tmp_path / "from-env"
