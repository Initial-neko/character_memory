from __future__ import annotations

from pathlib import Path

import pytest

from character_memory.voices import (
    VoiceProfile,
    VoiceProfileError,
    discover_voice_profiles,
    load_voice_profile,
)


def _persona_dir(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "persona.yaml").write_text(f"id: {name}\nname: {name}\n", encoding="utf-8")
    return directory


def _wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
    return path


def _write_voice(directory: Path, body: str) -> Path:
    target = directory / "voice.yaml"
    target.write_text(body, encoding="utf-8")
    return target


def test_load_voice_profile_absolute_ref_audio(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rin")
    audio = _wav(tmp_path / "shared" / "rin.wav")

    _write_voice(
        directory,
        f"ref_audio: {audio}\nref_text: '你好，我是凛。'\n",
    )

    profile = load_voice_profile(directory / "persona.yaml")

    assert isinstance(profile, VoiceProfile)
    assert profile.voice_id == "rin"
    assert profile.ref_audio == str(audio.resolve())
    assert Path(profile.ref_audio).is_absolute()
    assert profile.ref_text == "你好，我是凛。"
    assert profile.gpt_model is None
    assert profile.sovits_model is None


def test_load_voice_profile_relative_ref_audio_resolves_against_persona_dir(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "momo")
    audio = _wav(directory / "refs" / "momo.wav")

    _write_voice(directory, "ref_audio: refs/momo.wav\nref_text: hello\n")

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.ref_audio == str(audio.resolve())
    assert Path(profile.ref_audio).is_absolute()


def test_load_voice_profile_missing_file_returns_none(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")

    assert load_voice_profile(directory / "persona.yaml") is None


def test_load_voice_profile_empty_ref_text_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: ''\n")

    with pytest.raises(VoiceProfileError) as excinfo:
        load_voice_profile(directory / "persona.yaml")

    assert "voice.yaml" in str(excinfo.value)


def test_load_voice_profile_whitespace_ref_text_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: '   '\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_missing_ref_text_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_missing_ref_audio_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _write_voice(directory, "ref_text: hello\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_missing_ref_audio_file_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    missing = directory / "nope.wav"
    _write_voice(directory, "ref_audio: nope.wav\nref_text: hello\n")

    with pytest.raises(VoiceProfileError) as excinfo:
        load_voice_profile(directory / "persona.yaml")

    assert str(missing.resolve()) in str(excinfo.value)


def test_load_voice_profile_malformed_yaml_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _write_voice(directory, "ref_audio: [unclosed\nref_text: hello\n")

    with pytest.raises(VoiceProfileError) as excinfo:
        load_voice_profile(directory / "persona.yaml")

    assert "voice.yaml" in str(excinfo.value)


def test_load_voice_profile_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _write_voice(directory, "- ref_audio\n- ref_text\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_unknown_extra_key_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "rei")
    _wav(directory / "ref.wav")
    _write_voice(
        directory,
        "ref_audio: ref.wav\nref_text: hello\nref_audi: typo.wav\n",
    )

    with pytest.raises(VoiceProfileError) as excinfo:
        load_voice_profile(directory / "persona.yaml")

    assert "ref_audi" in str(excinfo.value)


def test_load_voice_profile_voice_id_defaults_to_directory_name(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: hello\n")

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.voice_id == "haru"


def test_load_voice_profile_default_voice_id_must_be_non_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _wav(tmp_path / "ref.wav")
    _write_voice(tmp_path, "ref_audio: ref.wav\nref_text: hello\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile("persona.yaml")


def test_load_voice_profile_empty_voice_id_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(directory, "voice_id: '  '\nref_audio: ref.wav\nref_text: hello\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_empty_ref_audio_string_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _write_voice(directory, "ref_audio: ''\nref_text: hello\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_wrong_type_rejected(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: 42\n")

    with pytest.raises(VoiceProfileError):
        load_voice_profile(directory / "persona.yaml")


def test_load_voice_profile_models_absent_are_none(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: hello\n")

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.gpt_model is None
    assert profile.sovits_model is None


def test_load_voice_profile_models_empty_string_are_none(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(
        directory,
        "ref_audio: ref.wav\nref_text: hello\ngpt_model: ''\nsovits_model: '   '\n",
    )

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.gpt_model is None
    assert profile.sovits_model is None


def test_load_voice_profile_models_are_stored_stripped_and_unresolved(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(
        directory,
        "ref_audio: ref.wav\nref_text: hello\ngpt_model: ' models/gpt.ckpt '\nsovits_model: models/sovits.pth\n",
    )

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.gpt_model == "models/gpt.ckpt"
    assert profile.sovits_model == "models/sovits.pth"


def test_load_voice_profile_ref_text_is_stripped(tmp_path: Path) -> None:
    directory = _persona_dir(tmp_path, "haru")
    _wav(directory / "ref.wav")
    _write_voice(directory, "ref_audio: ref.wav\nref_text: '  hello there  '\n")

    profile = load_voice_profile(directory / "persona.yaml")

    assert profile is not None
    assert profile.ref_text == "hello there"


def test_discover_voice_profiles_skips_personas_without_voice_yaml(tmp_path: Path) -> None:
    with_voice = _persona_dir(tmp_path, "rin")
    _wav(with_voice / "ref.wav")
    _write_voice(with_voice, "ref_audio: ref.wav\nref_text: hello\n")
    without_voice = _persona_dir(tmp_path, "momo")

    found = discover_voice_profiles([with_voice / "persona.yaml", without_voice / "persona.yaml"])

    assert set(found) == {"rin"}
    assert found["rin"].voice_id == "rin"


def test_discover_voice_profiles_empty_input(tmp_path: Path) -> None:
    assert discover_voice_profiles([]) == {}


def test_discover_voice_profiles_raises_on_duplicate_voice_id(tmp_path: Path) -> None:
    first = _persona_dir(tmp_path, "first")
    second = _persona_dir(tmp_path, "second")
    for directory in (first, second):
        _wav(directory / "ref.wav")
        _write_voice(directory, "voice_id: shared\nref_audio: ref.wav\nref_text: hello\n")

    with pytest.raises(VoiceProfileError) as excinfo:
        discover_voice_profiles([first / "persona.yaml", second / "persona.yaml"])

    message = str(excinfo.value)
    assert "shared" in message
    assert "first" in message
    assert "second" in message


def test_discover_voice_profiles_duplicate_check_is_order_independent(tmp_path: Path) -> None:
    first = _persona_dir(tmp_path, "first")
    second = _persona_dir(tmp_path, "second")
    for directory in (first, second):
        _wav(directory / "ref.wav")
        _write_voice(directory, "voice_id: shared\nref_audio: ref.wav\nref_text: hello\n")

    with pytest.raises(VoiceProfileError):
        discover_voice_profiles([second / "persona.yaml", first / "persona.yaml"])


def test_discover_voice_profiles_propagates_invalid_profile(tmp_path: Path) -> None:
    good = _persona_dir(tmp_path, "good")
    _wav(good / "ref.wav")
    _write_voice(good, "ref_audio: ref.wav\nref_text: hello\n")
    bad = _persona_dir(tmp_path, "bad")
    _write_voice(bad, "ref_audio: ref.wav\nref_text: ''\n")

    with pytest.raises(VoiceProfileError):
        discover_voice_profiles([good / "persona.yaml", bad / "persona.yaml"])
