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


def test_a_character_with_a_dead_reference_leaves_the_sidecar_unready(tmp_path):
    """Configured but broken is a config error, not an unconfigured character.

    It stays an error: the character does not quietly fall back to the default
    template, and no voice is served at all until it is fixed. It is reported
    rather than raised out of the constructor, so one persona's dead reference
    cannot take the rest of the stack down with it (spec §10).
    """

    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    (persona.parent / "voice.yaml").write_text("template: ghost\n", encoding="utf-8")

    status = _runtime(tmp_path).status()

    assert status["ready"] is False
    assert "haru references unknown template 'ghost'" in status["reason"]


def test_a_pre_template_voice_file_leaves_the_sidecar_unready_and_named(tmp_path):
    """No compatibility layer: the old form is an error, and a legible one.

    Nothing converts a self-contained ``voice.yaml`` any more, so this failure is
    the operator's only signal. It has to name the file and say that the fix is
    to build the clip into a template -- a bare "extra inputs are not permitted"
    tells them neither. It reaches them through ``/health`` rather than a process
    exit, because a bad file in the local voice tree is not grounds for taking
    the rest of the stack down with it (spec §10).
    """

    persona = tmp_path / "personas" / "haru" / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("id: haru\n", encoding="utf-8")
    voice_file = persona.parent / "voice.yaml"
    voice_file.write_text(
        "ref_audio: voice/x.wav\nref_text: 你好\n", encoding="utf-8"
    )

    status = _runtime(tmp_path).status()

    assert status["ready"] is False
    message = status["reason"]
    assert str(voice_file) in message
    assert "no longer read" in message
    assert "template" in message
    # ... and the way out names the template root this runtime actually reads,
    # not the module-level default.
    assert str(tmp_path / "voices") in message


def test_a_broken_unreferenced_template_names_itself_in_the_health_reason(tmp_path):
    """One bad template still stops every voice, but no longer the sidecar.

    The readers stay strict -- a template's clip must exist at load, referenced
    or not (spec §10) -- so nothing is skipped and no voice is silently
    downgraded to another clip. What the operator gets instead of a dead process
    is ``ready: false`` plus the offending path: with a healthy default template
    and a stale experiment nobody references, that path is the only symptom
    there is.
    """

    _template(tmp_path / "voices", "murasame")
    broken = tmp_path / "voices" / "old-experiment.yaml"
    broken.write_text(
        "ref_audio: old-experiment/missing.wav\nref_text: 旧实验。\n", encoding="utf-8"
    )

    status = _runtime(tmp_path, default_voice="murasame").status()

    assert status["ready"] is False
    message = status["reason"]
    assert str(broken) in message
    assert "ref_audio not found" in message


def test_a_broken_template_refuses_a_request_in_the_same_words_as_health(tmp_path):
    """``/health`` and the refusal must not disagree about why nothing works.

    A broken tree leaves the registry empty, so the refusal would otherwise
    report the default template as "missing or unusable" and send the operator
    looking for a file that is sitting right there under a name it just read --
    without ever saying which file in the tree is the one that does not parse.
    """

    _template(tmp_path / "voices", "murasame")
    broken = tmp_path / "voices" / "old-experiment.yaml"
    broken.write_text(
        "ref_audio: old-experiment/missing.wav\nref_text: 旧实验。\n", encoding="utf-8"
    )

    runtime = _runtime(tmp_path, default_voice="murasame")
    health = runtime.status()

    with TestClient(create_gsv_tts_app(runtime)) as client:
        response = client.post("/v1/tts", json={"text": "你好", "voice": "murasame"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert str(broken) in detail, detail
    assert "ref_audio not found" in detail, detail
    # Same cause, same words: the two surfaces are read side by side.
    assert health["reason"] in detail, (health["reason"], detail)


def test_a_fixed_voice_tree_reloads_back_to_ready(tmp_path):
    """The unready state has to be recoverable without a restart.

    dev_stack's whole reason for keeping the sidecar alive is that Settings
    Center can fix the tree in place, so a reload that succeeds has to clear the
    recorded failure -- otherwise the sidecar is up but permanently unready and
    the operator restarts the stack after all.
    """

    _template(tmp_path / "voices", "murasame")
    broken = tmp_path / "voices" / "old-experiment.yaml"
    broken.write_text(
        "ref_audio: old-experiment/missing.wav\nref_text: 旧实验。\n", encoding="utf-8"
    )

    runtime = _runtime(tmp_path, default_voice="murasame")
    assert runtime.status()["ready"] is False

    broken.unlink()
    reloaded = runtime.reload_voices()

    assert reloaded["ready"] is True, reloaded["reason"]
    assert reloaded["reason"] is None
