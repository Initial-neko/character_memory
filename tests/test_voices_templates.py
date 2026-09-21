"""Template-side readers.

A template is a named ``VoiceProfile`` under ``voices/``. It is the only thing
in the repo that owns a reference clip; a character only ever names one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from character_memory.voices import (
    VoiceProfile,
    VoiceProfileError,
    discover_character_voices,
    discover_templates,
    load_character_voice,
    load_template,
    resolve_voice_registry,
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


# --- character references ---------------------------------------------------


def _write_character(
    root: Path,
    character_id: str,
    document: dict | None,
    *,
    persona_id: str | None = None,
) -> Path:
    """``character_id`` names the directory; ``persona_id`` fills the ``id`` field.

    They are separate parameters because they are separate things in the app, and
    one case below depends on them disagreeing.
    """

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(
        f"id: {persona_id or character_id}\nname: {character_id}\n", encoding="utf-8"
    )
    if document is not None:
        import yaml

        (persona.parent / "voice.yaml").write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return persona


def test_load_character_voice_returns_none_without_a_file(tmp_path):
    persona = _write_character(tmp_path, "haru", None)

    assert load_character_voice(persona) is None


def test_load_character_voice_reads_the_template_name(tmp_path):
    persona = _write_character(tmp_path, "haru", {"template": "murasame"})

    assert load_character_voice(persona) == "murasame"


def test_load_character_voice_rejects_a_self_contained_profile(tmp_path):
    """The old form has no writer any more, and nothing migrates it either.

    A file in that shape is a hand-edit or a tree from before the template model,
    and a loud error is the intended outcome -- but it has to say which file and
    what to do, since no code path will do it for the operator.
    """

    persona = _write_character(
        tmp_path, "haru", {"ref_audio": "voice/x.wav", "ref_text": "你好"}
    )
    voices_root = tmp_path / "explicit-voices"

    with pytest.raises(VoiceProfileError) as excinfo:
        load_character_voice(persona, voices_root=voices_root)

    message = str(excinfo.value)
    assert "invalid voice profile" in message
    assert str(persona.parent / "voice.yaml") in message
    assert "template" in message and "no longer read" in message
    # The advice has to name the directory *this* reader was given. Falling back
    # to the module-level default points at a different tree whenever the caller
    # passed its own root -- which is what the sidecar does.
    assert str(voices_root) in message


def test_load_character_voice_requires_a_non_empty_name(tmp_path):
    persona = _write_character(tmp_path, "haru", {"template": "   "})

    with pytest.raises(VoiceProfileError, match="template is empty"):
        load_character_voice(persona)


def test_resolve_voice_registry_maps_a_character_onto_its_template(tmp_path):
    _write_template(tmp_path / "voices", "murasame")
    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "murasame"})

    registry = resolve_voice_registry(
        templates=discover_templates(tmp_path / "voices"),
        character_voices=discover_character_voices([persona]),
    )

    assert registry["haru"].ref_text == "你好，今天天气不错。"
    assert registry["haru"].voice_id == "haru"   # the character id wins
    assert registry["murasame"].voice_id == "murasame"  # the template stays addressable


def test_resolve_voice_registry_raises_when_a_referenced_template_is_missing(tmp_path):
    """A character that *has* a voice file but a dead reference is a config error."""

    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "ghost"})

    with pytest.raises(VoiceProfileError, match="haru references unknown template 'ghost'"):
        resolve_voice_registry(
            templates={},
            character_voices=discover_character_voices([persona]),
        )


def test_resolve_voice_registry_inherits_the_template_models(tmp_path):
    """A character cannot set models itself -- ``_CharacterVoiceDocument`` forbids
    it, because the same clip under two characters must not resolve to two
    different models. It inherits whatever the template declares."""
    _write_template(tmp_path / "voices", "murasame", gpt_model="base.ckpt")
    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "murasame"})

    registry = resolve_voice_registry(
        templates=discover_templates(tmp_path / "voices"),
        character_voices=discover_character_voices([persona]),
    )

    assert registry["haru"].gpt_model == "base.ckpt"


def test_discover_character_voices_keys_on_the_persona_id(tmp_path):
    """The browser sends ``voice: <profile["id"]>`` -- the ``id`` field, not the
    directory name. A registry keyed on the directory would never match that
    request and the character would fall back to the default template in
    silence, which is the failure this whole layer exists to prevent."""

    personas = tmp_path / "personas"
    persona = _write_character(
        personas, "haru", {"template": "murasame"}, persona_id="haruka"
    )

    assert discover_character_voices([persona]) == {"haruka": "murasame"}


def test_a_character_without_a_voice_file_stays_out_of_the_registry(tmp_path):
    """The other half of spec 5.2: never given a voice is normal, not an error."""

    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", None)

    assert discover_character_voices([persona]) == {}


def test_load_character_voice_rejects_a_document_that_is_not_a_mapping(tmp_path):
    """A list is the realistic typo (a stray ``- ``), and it must not read as
    'no voice configured' -- that would be the silent fallback this forbids."""

    persona = _write_character(tmp_path, "haru", None)
    (persona.parent / "voice.yaml").write_text("- murasame\n", encoding="utf-8")

    with pytest.raises(VoiceProfileError, match="expected a YAML mapping with a 'template' key"):
        load_character_voice(persona)


def test_discover_character_voices_rejects_two_personas_claiming_one_id(tmp_path):
    """Two directories may declare the same ``id``, and the app keeps whichever
    it finds first while dropping the rest. A voice cannot be attached to an id
    two personas claim: whichever one won, the other would be silently wrong."""

    personas = tmp_path / "personas"
    first = _write_character(personas, "haru", {"template": "murasame"})
    second = _write_character(
        personas, "haru-alt", {"template": "murasame"}, persona_id="haru"
    )

    with pytest.raises(VoiceProfileError, match="duplicate character id 'haru'"):
        discover_character_voices([first, second])


def test_discover_character_voices_skips_a_blank_persona_id(tmp_path, caplog):
    """A quoted blank ``id`` is present but unusable -- the directory fallback
    covers an *absent* field only. ``config.discover_character_profiles`` drops
    such a persona, so it is undiscoverable and no request can name it: nothing
    can get the wrong voice, and one unselectable character must not stop the
    sidecar from starting for all the others."""

    personas = tmp_path / "personas"
    persona = _write_character(personas, "haru", {"template": "murasame"})
    persona.write_text("id: '   '\nname: haru\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="character_memory.voices"):
        assert discover_character_voices([persona]) == {}

    assert "persona id is empty" in caplog.text
