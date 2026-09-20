"""The chat page's voice endpoints.

The logic is exercised as plain functions over tmp_path rather than through
HTTP. Every interesting failure -- a dead reference, a miscounted ``used_by``,
an id reaching the filesystem -- lives in the logic, and the routes are a thin
shell over it. The real proof that the wiring works is the manual verification
pass, not a string match on server.py.

The one boundary that does get a real round trip is writer-to-reader: this
module writes ``personas/<id>/voice.yaml`` and ``voices/<name>.yaml``, and the
sidecar reads them with ``voices.load_template`` / ``load_character_voice``.
That drift shipped once and failed silently, so it is asserted over real files
in the real locations rather than against an in-memory stand-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from character_memory.voice_web import voice_snapshot, write_character_voice
from character_memory.voices import load_character_voice, load_template


def _personas(root: Path, character_id: str, template: str | None, *, raw: str | None = None) -> Path:
    """Create a persona directory and return it."""

    persona = root / character_id / "persona.yaml"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text(f"id: {character_id}\nname: {character_id}\n", encoding="utf-8")
    if raw is not None:
        (persona.parent / "voice.yaml").write_text(raw, encoding="utf-8")
    elif template is not None:
        (persona.parent / "voice.yaml").write_text(
            yaml.safe_dump({"template": template}, sort_keys=False), encoding="utf-8"
        )
    return persona.parent


def _characters(*dirs: Path) -> list[dict]:
    """The shape ``discover_character_profiles`` returns, with only what we read."""

    return [{"id": d.name, "persona_path": str(d / "persona.yaml")} for d in dirs]


def _template(root: Path, name: str) -> None:
    audio = root / name / "clip.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "ref_audio": str(audio),
                "ref_text": f"{name} 的参考文本",
                "created_at": "2026-09-20T11:26:53+00:00",
                "instruct": "可爱萝莉音",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_snapshot_lists_templates_with_their_provenance(tmp_path):
    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", "murasame")

    snapshot = voice_snapshot(characters=_characters(haru), voices_root=voices)

    entry = next(item for item in snapshot["templates"] if item["name"] == "murasame")
    assert entry["ref_text"] == "murasame 的参考文本"
    assert entry["instruct"] == "可爱萝莉音"
    assert entry["used_by"] == ["haru"]


def test_snapshot_counts_every_character_that_references_a_template(tmp_path):
    """This is what makes the overwrite warning in the spec possible."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", "murasame")
    momo = _personas(tmp_path / "personas", "momo", "murasame")

    snapshot = voice_snapshot(characters=_characters(haru, momo), voices_root=voices)

    entry = next(item for item in snapshot["templates"] if item["name"] == "murasame")
    assert entry["used_by"] == ["haru", "momo"]


def test_snapshot_separates_unset_from_missing(tmp_path):
    """The whole point: never conflate 'not configured' with 'misconfigured'."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(tmp_path / "personas", "haru", None)  # never given a voice
    momo = _personas(tmp_path / "personas", "momo", "ghost")  # points at a template that is gone

    snapshot = voice_snapshot(characters=_characters(haru, momo), voices_root=voices)

    assert snapshot["characters"]["haru"]["status"] == "unset"
    assert snapshot["characters"]["haru"]["error"] is None
    assert snapshot["characters"]["momo"]["status"] == "missing"
    assert "ghost" in snapshot["characters"]["momo"]["error"]


def test_snapshot_reports_a_character_still_in_the_old_shape(tmp_path):
    """The pre-template form must be refused, not silently read.

    Nothing writes ``ref_audio``/``ref_text`` into a character's file any more --
    the clip lives in a template now -- so a file like this is either hand-written
    or left over from somewhere else. ``load_character_voice`` declares neither
    key and forbids extras, so it is an error rather than a voice, and the
    message has to name the way out: an operator who only reads "extra fields"
    has no idea what to do next.
    """

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    haru = _personas(
        tmp_path / "personas", "haru", None, raw="ref_audio: voice/x.wav\nref_text: 你好\n"
    )

    snapshot = voice_snapshot(characters=_characters(haru), voices_root=voices)

    entry = snapshot["characters"]["haru"]
    assert entry["status"] == "error"
    assert "pre-template form" in entry["error"]
    assert "template:" in entry["error"]


def test_reading_an_old_shape_file_raises_rather_than_returning_a_template(tmp_path):
    """The same refusal at the reader, without the snapshot in the way."""

    persona_dir = _personas(
        tmp_path / "personas", "haru", None, raw="ref_audio: voice/x.wav\nref_text: 你好\n"
    )

    with pytest.raises(ValueError):
        load_character_voice(persona_dir / "persona.yaml")


def test_a_broken_template_is_reported_without_blanking_the_list(tmp_path):
    """One unusable template must not cost the user the whole picker."""

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    (voices / "broken.yaml").write_text("ref_text: 有文本但没音频\n", encoding="utf-8")

    snapshot = voice_snapshot(characters=[], voices_root=voices)

    assert [item["name"] for item in snapshot["templates"]] == ["murasame"]
    assert any("broken" in note for note in snapshot["errors"])


def test_snapshot_on_empty_input_is_not_an_error(tmp_path):
    snapshot = voice_snapshot(characters=[], voices_root=tmp_path / "nope")

    assert snapshot == {"templates": [], "characters": {}, "errors": []}


def test_writing_a_reference_leaves_only_the_one_line(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template="murasame", voices_root=voices)

    written = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
    assert written == {"template": "murasame"}
    assert not list(persona_dir.glob("*.tmp"))


def test_the_written_reference_round_trips_through_the_strict_reader(tmp_path):
    """Writer to reader, across the module boundary, over the real file."""

    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template="murasame", voices_root=voices)

    assert load_character_voice(persona_dir / "persona.yaml") == "murasame"
    # And the name it references is one the template reader resolves.
    assert load_template(voices / "murasame.yaml").voice_id == "murasame"


def test_writing_replaces_an_existing_reference(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", "murasame")
    _template(voices, "murasame")
    _template(voices, "haru")

    write_character_voice(persona_dir=persona_dir, template="haru", voices_root=voices)

    written = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
    assert written == {"template": "haru"}


def test_clearing_a_reference_deletes_the_file(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", "murasame")
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template=None, voices_root=voices)

    assert not (persona_dir / "voice.yaml").exists()


def test_clearing_an_absent_reference_is_not_an_error(tmp_path):
    """The drawer posts None for a character that never had a voice; that is fine."""

    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    _template(voices, "murasame")

    write_character_voice(persona_dir=persona_dir, template=None, voices_root=voices)

    assert not (persona_dir / "voice.yaml").exists()


def test_writing_an_unknown_template_is_refused(tmp_path):
    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)

    with pytest.raises(ValueError, match="ghost"):
        write_character_voice(persona_dir=persona_dir, template="ghost", voices_root=voices)
    assert not (persona_dir / "voice.yaml").exists()


def test_a_template_name_is_not_a_path(tmp_path):
    """The name comes from the browser and becomes voices/<name>; it is not a path."""

    voices = tmp_path / "voices"
    persona_dir = _personas(tmp_path / "personas", "haru", None)

    with pytest.raises(ValueError):
        write_character_voice(
            persona_dir=persona_dir, template="../../etc/passwd", voices_root=voices
        )
    assert not (persona_dir / "voice.yaml").exists()


class _StubReloader:
    """Never talks to a sidecar; the reload result is not what these assert."""

    def __init__(self, *args, **kwargs):
        pass

    def reload(self) -> None:
        pass


def _attached_app(tmp_path, monkeypatch):
    """A minimal app carrying just what ``attach_voice_routes`` needs.

    The routes are the contract the browser actually calls, so they get one real
    HTTP pass each: the pure functions above cannot catch a path typo, and a
    string match on server.py cannot catch one either.
    """

    from types import SimpleNamespace

    from fastapi import FastAPI

    from character_memory.config import Settings
    from character_memory.voice_web import attach_voice_routes

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    monkeypatch.setattr("character_memory.tts_lab.GsvVoiceReloader", _StubReloader)
    app = FastAPI()
    app.state.character_memory = SimpleNamespace(
        settings=Settings(persona_path=str(tmp_path / "personas" / "haru" / "persona.yaml"))
    )
    attach_voice_routes(app)
    return app


def test_the_route_serves_the_snapshot_the_drawer_reads(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    _personas(tmp_path / "personas", "haru", "murasame")

    response = TestClient(_attached_app(tmp_path, monkeypatch)).get("/v1/voice-templates")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["templates"]] == ["murasame"]
    assert body["templates"][0]["used_by"] == ["haru"]
    assert body["characters"]["haru"] == {
        "template": "murasame",
        "status": "set",
        "error": None,
    }


def test_the_route_writes_a_reference_the_sidecar_can_read(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    client = TestClient(_attached_app(tmp_path, monkeypatch))

    response = client.post("/v1/characters/haru/voice", json={"template": "murasame"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert load_character_voice(persona_dir / "persona.yaml") == "murasame"


def test_the_route_clears_a_reference(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    persona_dir = _personas(tmp_path / "personas", "haru", "murasame")
    client = TestClient(_attached_app(tmp_path, monkeypatch))

    response = client.post("/v1/characters/haru/voice", json={"template": None})

    assert response.status_code == 200
    assert not (persona_dir / "voice.yaml").exists()


def test_the_route_refuses_an_unknown_character(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    _template(tmp_path / "voices", "murasame")
    _personas(tmp_path / "personas", "haru", None)
    client = TestClient(_attached_app(tmp_path, monkeypatch))

    response = client.post("/v1/characters/nobody/voice", json={"template": "murasame"})

    assert response.status_code == 404
    assert "nobody" in response.text


def test_the_route_refuses_an_unknown_template(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    voices = tmp_path / "voices"
    _template(voices, "murasame")
    persona_dir = _personas(tmp_path / "personas", "haru", None)
    client = TestClient(_attached_app(tmp_path, monkeypatch))

    response = client.post("/v1/characters/haru/voice", json={"template": "ghost"})

    assert response.status_code == 404
    assert "ghost" in response.text
    assert not (persona_dir / "voice.yaml").exists()


def test_server_attaches_the_voice_routes():
    """A weak wiring check, same shape as test_avatar_web_assets.py.

    It cannot prove the route works -- the manual verification pass does that.
    It catches one thing only: the module was written but never registered.
    """

    server = (
        Path(__file__).resolve().parents[1] / "src" / "character_memory" / "server.py"
    ).read_text(encoding="utf-8")

    assert "attach_voice_routes(app)" in server
