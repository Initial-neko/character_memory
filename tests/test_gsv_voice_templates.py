"""The sidecar's view of the template registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from character_memory.gsv_tts_experiment import GsvTtsRuntime, create_gsv_tts_app


def _template(root: Path, name: str) -> None:
    """Write a template the way production does: clip beside it, named relatively.

    Relative is the write contract -- the freeze route puts the clip in the
    template's own directory and names it from there -- so that is the form the
    readers' anchor (the document's parent) has to be tested against. The
    absolute form stays covered by ``tests/test_voices_templates.py``; writing
    both here would leave neither shape pinned.
    """

    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"ref_audio": f"{name}/{audio.name}", "ref_text": f"{name} 的参考文本"},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _runtime(tmp_path: Path, **kwargs) -> GsvTtsRuntime:
    """A runtime whose base models exist on disk, so only the template varies.

    The models are written here rather than stubbed because ``_asset_status``
    checks ``Path(...).is_file()``; a relative path would resolve against the
    cwd and the check would fail for the wrong reason.
    """

    ckpt = tmp_path / "base.ckpt"
    ckpt.write_bytes(b"ckpt")
    sovits = tmp_path / "base.pth"
    sovits.write_bytes(b"pth")
    return GsvTtsRuntime(
        gpt_model=str(ckpt),
        sovits_model=str(sovits),
        tts_factory=lambda **_: None,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        **kwargs,
    )


def test_registry_merges_templates_and_character_references(tmp_path):
    _template(tmp_path / "voices", "murasame")
    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text("template: murasame\n", encoding="utf-8")

    runtime = _runtime(tmp_path)

    assert set(runtime._voices) == {"murasame", "haru"}
    voice, ref_audio, ref_text, _, _ = runtime._resolve_voice("haru")
    assert voice == "haru"
    assert ref_text == "murasame 的参考文本"


def test_voice_ids_lists_templates_so_settings_can_offer_them(tmp_path):
    _template(tmp_path / "voices", "murasame")

    runtime = _runtime(tmp_path, default_voice="murasame")

    assert runtime._voice_ids() == ["murasame"]


def test_asset_status_requires_the_default_template_to_resolve(tmp_path):
    """Readiness moved from 'four legacy env fields' to 'the default template works'."""

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")

    ready, reason = runtime._asset_status()

    assert ready is True, reason


def test_asset_status_reports_a_missing_default_template(tmp_path):
    runtime = _runtime(tmp_path, default_voice="ghost")

    ready, reason = runtime._asset_status()

    assert ready is False
    assert "ghost" in reason


def test_a_missing_default_template_refuses_the_request_loudly(tmp_path):
    """Asserted through the request path, not through ``_resolve_voice``.

    Neither the requested name nor the default template resolves, and this is
    what the operator actually meets. The old fallback would have answered from
    the runtime-global reference -- a silent wrong voice, and reachable whenever
    the engine was already warm. Failing loudly only helps if the message names
    both the voice that was asked for and the template that would have served
    it, since the real cause is a template that was never created.
    """
    runtime = _runtime(tmp_path, default_voice="ghost")

    with TestClient(create_gsv_tts_app(runtime)) as client:
        response = client.post("/v1/tts", json={"text": "你好", "voice": "momo"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "momo" in detail, detail
    assert "ghost" in detail, detail
    assert "missing" in detail, detail


def test_a_character_with_no_voice_file_degrades_silently(tmp_path):
    """Not configured is fine; the browser sends an id for every character."""

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")

    voice, _, ref_text, _, _ = runtime._resolve_voice("someone-with-no-voice")

    assert voice == "murasame"
    assert ref_text == "murasame 的参考文本"


def test_the_fallback_never_uses_the_legacy_reference_fields(tmp_path):
    """The sharp version of the test above.

    The settings page stopped driving ``GSV_TTS_REF_AUDIO``/``_REF_TEXT``, and
    the runtime-global pair they fed is gone from the sidecar entirely: there is
    no attribute left to read, so a fallback cannot come back without the test
    failing. The old one returned those two values directly, which would hand
    GSV a blank reference for every character with no voice -- a silent, total
    breakage. Assert the attributes do not exist *and* that a real reference
    still comes back, so the test cannot pass by accident.
    """

    _template(tmp_path / "voices", "murasame")
    runtime = _runtime(tmp_path, default_voice="murasame")
    assert not hasattr(runtime, "ref_audio")
    assert not hasattr(runtime, "ref_text")

    _, ref_audio, ref_text, _, _ = runtime._resolve_voice("someone-with-no-voice")

    assert ref_audio.endswith("clip.wav")
    assert ref_text == "murasame 的参考文本"


def test_a_character_with_a_dead_reference_raises_at_load(tmp_path):
    """Configured but broken is a config error, not an unconfigured character."""

    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text("template: ghost\n", encoding="utf-8")

    from character_memory.voices import VoiceProfileError

    with pytest.raises(VoiceProfileError, match="haru references unknown template 'ghost'"):
        _runtime(tmp_path)


def test_a_pre_template_voice_file_refuses_to_start_the_sidecar(tmp_path):
    """No compatibility layer: the old form is an error, and a legible one.

    Nothing converts a self-contained ``voice.yaml`` any more, so this failure is
    the operator's only signal. It has to name the file and say that the fix is
    to build the clip into a template -- a bare "extra inputs are not permitted"
    tells them neither.
    """

    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    voice_file = persona.parent / "voice.yaml"
    voice_file.write_text(
        "ref_audio: voice/x.wav\nref_text: 你好\n", encoding="utf-8"
    )

    from character_memory.voices import VoiceProfileError

    with pytest.raises(VoiceProfileError) as excinfo:
        _runtime(tmp_path)

    message = str(excinfo.value)
    assert str(voice_file) in message
    assert "no longer read" in message
    assert "template" in message
    # ... and the way out names the template root this runtime actually reads,
    # not the module-level default.
    assert str(tmp_path / "voices") in message


def test_a_broken_unreferenced_template_names_itself_in_the_error(tmp_path):
    """One bad template stops every voice, and the error has to say which one.

    Narrowing that blast radius is not this unit's to do (spec §10: a template's
    clip must exist at load, referenced or not). The whole cost of the behaviour
    is legibility, so the failure has to name the offending file: with a healthy
    default template and a stale experiment nobody references, "the sidecar will
    not start" is the only symptom the operator gets.
    """

    _template(tmp_path / "voices", "murasame")
    broken = tmp_path / "voices" / "old-experiment.yaml"
    broken.write_text(
        "ref_audio: old-experiment/missing.wav\nref_text: 旧实验。\n", encoding="utf-8"
    )

    from character_memory.voices import VoiceProfileError

    with pytest.raises(VoiceProfileError) as excinfo:
        _runtime(tmp_path, default_voice="murasame")

    message = str(excinfo.value)
    assert str(broken) in message
    assert "ref_audio not found" in message
