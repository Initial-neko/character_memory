"""Free-naming a template, and the string rules that keep it a filename.

The name rule is a pure function, so most of this file exercises it directly.
The route that uses it is then tested against the real filesystem, because the
failure that matters is not "did it answer 200" but "can the strict reader that
GSV itself uses read back what the route wrote" -- a shape only its writer
agrees with is a template the sidecar never loads, silently.

The fake sidecar rig is imported from ``test_tts_lab_voice_freeze``: it is the
same app under test, and a second copy of the fakes is a second thing to drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from character_memory.tts_lab import validate_template_name
from character_memory.voices import load_template, template_root

from test_tts_lab_voice_freeze import _build_app, _generate, _persona_root, _write_persona


def _save(client: TestClient, *, artifact_id: str, name: str):
    return client.post(
        "/v1/voice-design/save-template", json={"artifact_id": artifact_id, "name": name}
    )


def _save_rig(tmp_path, monkeypatch):
    """A lab app whose template tree is the tmp path, plus its fake sidecar."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    return TestClient(app), voice_client


# --- the name rule -----------------------------------------------------------


@pytest.mark.parametrize("name", ["murasame", "haru-2", "中文名", "a_b"])
def test_accepts_reasonable_names(name):
    assert validate_template_name(f"  {name}  ") == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a/b",
        "a\\b",
        "..",
        "../etc/passwd",
        "/abs/path",
        "C:/windows",
        "con",
        "PRN",
        "com1",
        "lpt9",
        "trailing.",
    ],
)
def test_rejects_names_that_would_break_the_write(name):
    """Every rule maps to a real failure: these become voices/<name>.yaml and a directory."""

    with pytest.raises(ValueError):
        validate_template_name(name)


def test_wrapping_whitespace_is_trimmed_rather_than_rejected():
    """The strip runs first, so a trailing space is a name that still works.

    A trailing *dot* is the one that has to be refused: Windows silently drops
    it, so ``momo.`` and ``momo`` would be two names for one file.
    """

    assert validate_template_name("trailing ") == "trailing"


def test_rejects_an_overlong_name():
    with pytest.raises(ValueError, match="too long"):
        validate_template_name("x" * 65)


# --- the route ---------------------------------------------------------------


def test_saving_writes_a_template_the_sidecar_reader_can_read(tmp_path, monkeypatch):
    """Writer to reader across the module boundary, over real files and real paths."""

    client, _ = _save_rig(tmp_path, monkeypatch)
    audio, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")

    response = _save(client, artifact_id=artifact_id, name="murasame")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["template"] == "murasame"
    assert body["ref_text"] == "你好，这是试听。"
    assert body["activated"] is True

    profile = load_template(tmp_path / "voices" / "murasame.yaml")
    assert profile.voice_id == "murasame"
    assert profile.ref_text == "你好，这是试听。"
    assert Path(profile.ref_audio).read_bytes() == audio


def test_saving_needs_no_character(tmp_path, monkeypatch):
    """The point of the free-naming path: a reusable voice with no owner yet."""

    client, _ = _save_rig(tmp_path, monkeypatch)
    _, artifact_id = _generate(client, text="你好", instruct="可爱萝莉音")

    assert _save(client, artifact_id=artifact_id, name="murasame").status_code == 200

    assert sorted(path.name for path in (tmp_path / "voices").iterdir()) == [
        "murasame",
        "murasame.yaml",
    ]
    # Nothing was written into a persona: this route knows no character.
    assert not (_persona_root(tmp_path) / "momo" / "voice.yaml").exists()


def test_saving_over_an_existing_template_is_refused(tmp_path, monkeypatch):
    """A typed name colliding is a mistake, not an intended overwrite."""

    client, voice_client = _save_rig(tmp_path, monkeypatch)
    _, first = _generate(client, text="第一句", instruct="可爱萝莉音")
    assert _save(client, artifact_id=first, name="murasame").status_code == 200

    voice_client.audio = b"RIFFvoice-design-b"
    _, second = _generate(client, text="第二句", instruct="可爱萝莉音")
    response = _save(client, artifact_id=second, name="murasame")

    assert response.status_code == 409
    assert "murasame" in response.text
    document = yaml.safe_load((tmp_path / "voices" / "murasame.yaml").read_text(encoding="utf-8"))
    assert document["ref_text"] == "第一句"
    assert len(list((tmp_path / "voices" / "murasame").glob("*.wav"))) == 1


@pytest.mark.parametrize("name", ["a/b", "../etc/passwd", "con", "trailing."])
def test_the_route_refuses_a_name_that_is_not_a_filename(tmp_path, monkeypatch, name):
    client, _ = _save_rig(tmp_path, monkeypatch)
    _, artifact_id = _generate(client, text="你好", instruct="可爱萝莉音")

    response = _save(client, artifact_id=artifact_id, name=name)

    assert response.status_code == 400
    assert not (tmp_path.parent / "etc").exists()
    assert not (tmp_path / "voices").exists()


def test_the_route_refuses_an_unknown_artifact(tmp_path, monkeypatch):
    client, _ = _save_rig(tmp_path, monkeypatch)

    response = _save(client, artifact_id="not-a-real-token", name="murasame")

    assert response.status_code == 404
    assert "generate" in response.text.lower()
    assert not (tmp_path / "voices").exists()


def test_saving_rejects_extra_fields(tmp_path, monkeypatch):
    """A client must not be able to smuggle a transcript past the audition."""

    client, _ = _save_rig(tmp_path, monkeypatch)
    _, artifact_id = _generate(client, text="你好", instruct="可爱萝莉音")

    response = client.post(
        "/v1/voice-design/save-template",
        json={"artifact_id": artifact_id, "name": "murasame", "ref_text": "smuggled"},
    )

    assert response.status_code == 422


def test_both_writers_agree_on_where_templates_live(tmp_path, monkeypatch):
    """Freezing and saving share one writer, so they resolve one root.

    ``template_root(None)`` falls back to a cwd-relative ``voices``, which is the
    failure the start script exists to prevent: the writer would file templates
    where the sidecar is not looking. Pinning that both routes read the same
    environment variable is what keeps that from being a silent split.
    """

    client, _ = _save_rig(tmp_path, monkeypatch)
    _, artifact_id = _generate(client, text="你好", instruct="可爱萝莉音")
    assert _save(client, artifact_id=artifact_id, name="murasame").status_code == 200

    assert template_root(None) == tmp_path / "voices"
