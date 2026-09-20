"""VoiceDesign audition -> freeze contract.

VoiceDesign is unseeded: four identical requests produced different durations.
Freezing therefore persists the exact WAV bytes that were auditioned, never a
re-synthesis. These tests pin that down against the real filesystem.

Freezing writes three things, in this order: the clip under ``voices/<name>/``,
the template document ``voices/<name>.yaml``, and finally the character's own
``personas/<id>/voice.yaml`` reference. The order is the point -- a crash may
leave an orphaned file, never a reference to a template with no audio.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from character_memory.config import Settings
from character_memory.tts_lab import (
    GsvVoiceReloader,
    Qwen3VoiceDesignSidecar,
    TtsLabRuntime,
    create_tts_lab_app,
)
from character_memory.voices import (
    discover_character_voices,
    discover_templates,
    load_character_voice,
    load_template,
    resolve_voice_registry,
)

VOICE_DESIGN_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"


class _FakeResponse:
    def __init__(self, payload=None, *, content=b"", headers=None, status_code=200):
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.text = ""

    @property
    def is_error(self):
        return self.status_code >= 400

    def raise_for_status(self):
        if self.is_error:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeVoiceDesignClient:
    """Returns whatever audio the test set last, and counts calls."""

    def __init__(self, audio: bytes = b"RIFFvoice-design-a"):
        self.audio = audio
        self.posts: list = []

    def get(self, *args, **kwargs):
        return _FakeResponse(
            {
                "ready": True,
                "loaded": True,
                "model": VOICE_DESIGN_MODEL,
                "device": "cuda:0",
                "reason": None,
            }
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResponse(
            content=self.audio,
            headers={
                "content-type": "audio/wav",
                "x-voice-design-model": VOICE_DESIGN_MODEL,
                "x-voice-design-device": "cuda:0",
                "x-voice-design-inference-ms": "3210.5",
                "x-voice-design-audio-ms": "2800",
                "x-voice-design-sample-rate": "24000",
            },
        )


class _FakeRegistryClient:
    """Stands in for the GSV sidecar's voice registry."""

    def __init__(self, *, status_code: int = 200, error: Exception | None = None):
        self.status_code = status_code
        self.error = error
        self.posts: list = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return _FakeResponse({}, status_code=self.status_code)


class FakeTtsProvider:
    def status(self) -> dict:
        return {"id": "fake", "label": "Fake TTS", "ready": True, "loaded": True}

    def synthesize(self, text: str, *, voice: str, speed: float):
        raise AssertionError("freeze must never re-synthesize")


def _write_persona(root: Path, character_id: str) -> Path:
    directory = root / character_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "persona.yaml").write_text(
        f"id: {character_id}\nname: {character_id.title()}\n",
        encoding="utf-8",
    )
    return directory


def _persona_root(tmp_path: Path) -> Path:
    """A dedicated persona root so escapes land inside tmp_path and are assertable."""
    return tmp_path / "personas"


def _build_app(tmp_path: Path, *, registry_client: _FakeRegistryClient | None = None):
    voice_client = _FakeVoiceDesignClient()
    reloader = GsvVoiceReloader(client=registry_client or _FakeRegistryClient())
    # discover_character_profiles() globs "<root>/*/persona.yaml" where root is the
    # grandparent of settings.persona_path, so tmp_path/personas/momo/persona.yaml
    # makes tmp_path/personas the persona root.
    app = create_tts_lab_app(
        TtsLabRuntime({"fake": FakeTtsProvider()}),
        voice_design=Qwen3VoiceDesignSidecar(client=voice_client),
        voice_reloader=reloader,
        settings_factory=lambda: Settings(
            persona_path=str(_persona_root(tmp_path) / "momo" / "persona.yaml")
        ),
    )
    return app, voice_client


def _generate(client: TestClient, *, text: str, instruct: str) -> tuple[bytes, str]:
    response = client.post(
        "/v1/voice-design/generate",
        json={"text": text, "language": "Chinese", "instruct": instruct, "max_new_tokens": 2048},
    )
    assert response.status_code == 200
    return response.content, response.headers["x-voice-design-artifact"]


def _freeze(client: TestClient, *, character_id: str, artifact_id: str):
    return client.post(
        "/v1/voice-design/freeze",
        json={"character_id": character_id, "artifact_id": artifact_id},
    )


def _tree(root: Path) -> set[str]:
    return {
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file()
    }


# --- reloader contract -------------------------------------------------------


def test_gsv_voice_reloader_uses_env_base_url(monkeypatch):
    monkeypatch.setenv("GSV_TTS_BASE_URL", "http://127.0.0.1:9999")
    client = _FakeRegistryClient()
    GsvVoiceReloader(client=client).reload()
    assert [url for url, _ in client.posts] == ["http://127.0.0.1:9999/v1/voices/reload"]


def test_gsv_voice_reloader_defaults_to_port_9014(monkeypatch):
    monkeypatch.delenv("GSV_TTS_BASE_URL", raising=False)
    client = _FakeRegistryClient()
    GsvVoiceReloader(client=client).reload()
    assert [url for url, _ in client.posts] == ["http://127.0.0.1:9014/v1/voices/reload"]


def test_gsv_voice_reloader_raises_on_http_error():
    with pytest.raises(RuntimeError):
        GsvVoiceReloader(client=_FakeRegistryClient(status_code=404)).reload()


# --- generate keeps the artifact --------------------------------------------


def test_generate_returns_an_artifact_token(tmp_path):
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        assert audio == b"RIFFvoice-design-a"
        assert token


def test_artifact_tokens_are_not_derived_from_content(tmp_path):
    """Identical audio must not collapse into one token; the caller never picks it."""
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, first = _generate(client, text="同一句话", instruct="同一条描述")
        _, second = _generate(client, text="同一句话", instruct="同一条描述")
        assert first != second


# --- freeze ------------------------------------------------------------------


def test_freeze_writes_the_template_with_the_frozen_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线，清亮柔和。")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    digest = hashlib.sha256(audio).hexdigest()[:16]
    relative = f"momo/{digest}.wav"
    body = response.json()
    assert body == {
        "ok": True,
        "character_id": "momo",
        "voice_id": "momo",
        "template": "momo",
        "ref_audio": relative,
        "ref_text": "你好，这是试听。",
        "shared_with": [],
        "activated": True,
        "reason": None,
    }

    document = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    assert document == {
        "voice_id": "momo",
        "ref_audio": relative,
        "ref_text": "你好，这是试听。",
        "gpt_model": None,
        "sovits_model": None,
        "created_at": document["created_at"],
        "instruct": "年轻女性声线，清亮柔和。",
        "model": VOICE_DESIGN_MODEL,
    }
    created_at = datetime.fromisoformat(document["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset().total_seconds() == 0

    # The character's own file is a reference and nothing else: the clip lives
    # in the template tree, so a character can never hold a second copy of it.
    assert yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8")) == {
        "template": "momo"
    }


def test_freezing_writes_a_template_then_a_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")

    response = _freeze(client, character_id="momo", artifact_id=artifact_id)

    assert response.status_code == 200
    template = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    # The reference text is the string the audio was synthesized from, so it is
    # exact by construction -- no human transcription, which is the failure mode
    # that makes zero-shot cloning come out wrong.
    assert template["ref_text"] == "你好，这是试听。"
    assert template["instruct"] == "可爱萝莉音"
    # ref_audio is relative to the template file, so resolve it the same way
    # load_template does rather than against the cwd.
    assert (tmp_path / "voices" / template["ref_audio"]).is_file()

    reference = yaml.safe_load(
        (_persona_root(tmp_path) / "momo" / "voice.yaml").read_text(encoding="utf-8")
    )
    assert reference == {"template": "momo"}


def test_freezing_writes_exactly_three_files(tmp_path, monkeypatch):
    """Pin the write set, so a stray or half-written file cannot appear unnoticed."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")

    _freeze(client, character_id="momo", artifact_id=artifact_id)

    written = _tree(tmp_path)
    clips = {name for name in written if name.startswith("voices/momo/")}

    assert len(clips) == 1 and next(iter(clips)).endswith(".wav")
    # The persona file is the fixture, not a freeze output.
    assert written - clips - {"personas/momo/persona.yaml"} == {
        "voices/momo.yaml",
        "personas/momo/voice.yaml",
    }


def test_the_frozen_template_round_trips_through_the_reader(tmp_path, monkeypatch):
    """Writer to reader, across the module boundary. This drift shipped once."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=artifact_id)

    profile = load_template(tmp_path / "voices" / "momo.yaml")

    assert profile.voice_id == "momo"
    assert profile.ref_text == "你好，这是试听。"
    assert Path(profile.ref_audio).is_file()

    # And the reference the route wrote is readable by the strict reader too --
    # a template nobody references is a voice the chat page never uses.
    referenced = load_character_voice(_persona_root(tmp_path) / "momo" / "persona.yaml")
    assert referenced == "momo"


def test_the_frozen_voice_resolves_for_the_character_in_the_registry(tmp_path, monkeypatch):
    """The end of the chain: freeze -> both readers -> the sidecar's registry.

    This is the whole feature in one assertion. Each hop has its own test; what
    this catches is a shape that round-trips through each reader alone but does
    not merge -- a character whose reference names a template the registry does
    not carry leaves the sidecar permanently not ready, silently.
    """

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    client = TestClient(app)
    _, artifact_id = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=artifact_id)

    registry = resolve_voice_registry(
        templates=discover_templates(tmp_path / "voices"),
        character_voices=discover_character_voices(
            [persona_dir / "persona.yaml"], voices_root=tmp_path / "voices"
        ),
    )

    assert registry["momo"].voice_id == "momo"
    assert registry["momo"].ref_text == "你好，这是试听。"
    assert Path(registry["momo"].ref_audio).is_file()


def test_a_hand_written_template_survives_an_unquoted_created_at(tmp_path):
    """``created_at`` is written quoted, but a hand-edited file is not.

    YAML turns an unquoted ISO timestamp into a datetime, so the reader has to
    accept both spellings rather than pin the field to ``str``.
    """
    voices = tmp_path / "voices"
    (voices / "momo").mkdir(parents=True)
    (voices / "momo" / "clip.wav").write_bytes(b"RIFFclip")
    (voices / "momo.yaml").write_text(
        "ref_audio: momo/clip.wav\n"
        "ref_text: 你好\n"
        "created_at: 2026-09-19T15:26:34.472897+00:00\n",
        encoding="utf-8",
    )

    profile = load_template(voices / "momo.yaml")

    assert profile.voice_id == "momo"


def test_freezing_twice_overwrites_the_template_but_keeps_both_clips(tmp_path, monkeypatch):
    """The old WAV stays as an orphan on purpose.

    Content addressing is what makes this safe: a design cannot be regenerated,
    so overwriting the clip an older template still pointed at would lose a
    voice permanently. An orphan only costs disk.
    """

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, voice_client = _build_app(tmp_path)
    _write_persona(_persona_root(tmp_path), "momo")
    client = TestClient(app)

    _, first = _generate(client, text="第一次", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=first)
    voice_client.audio = b"RIFFvoice-design-b"
    _, second = _generate(client, text="第二次", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=second)

    template = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    assert template["ref_text"] == "第二次"
    assert len(list((tmp_path / "voices" / "momo").glob("*.wav"))) == 2


def test_freezing_reports_other_characters_sharing_the_template(tmp_path, monkeypatch):
    """Spec 7.1: overwriting changes their voice too, so the caller must be told."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    app, _ = _build_app(tmp_path)
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo")
    _write_persona(personas, "haru")
    client = TestClient(app)

    _, first = _generate(client, text="你好，这是试听。", instruct="可爱萝莉音")
    _freeze(client, character_id="momo", artifact_id=first)
    # haru now points at momo's template, so re-freezing momo affects haru too.
    (personas / "haru" / "voice.yaml").write_text("template: momo\n", encoding="utf-8")

    _, second = _generate(client, text="第二次", instruct="可爱萝莉音")
    response = _freeze(client, character_id="momo", artifact_id=second)

    assert response.json()["shared_with"] == ["haru"]


def test_frozen_wav_is_exactly_the_auditioned_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        assert _freeze(client, character_id="momo", artifact_id=token).status_code == 200
        written = (
            tmp_path / "voices" / "momo" / f"{hashlib.sha256(audio).hexdigest()[:16]}.wav"
        ).read_bytes()

    assert written == audio
    assert written == b"RIFFvoice-design-a"
    # Exactly one VoiceDesign call: freeze persisted the audition, it did not re-synthesize.
    assert len(voice_client.posts) == 1


def test_ref_text_is_the_text_that_produced_the_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="第一句试听文本。", instruct="年轻女性声线")
        assert _freeze(client, character_id="momo", artifact_id=token).status_code == 200
        voice_client.audio = b"RIFFvoice-design-b"
        _, second = _generate(client, text="第二句完全不同的文本。", instruct="低沉男声")
        assert _freeze(client, character_id="momo", artifact_id=second).status_code == 200

    document = yaml.safe_load((tmp_path / "voices" / "momo.yaml").read_text(encoding="utf-8"))
    assert document["ref_text"] == "第二句完全不同的文本。"
    assert document["instruct"] == "低沉男声"


def test_refreeze_keeps_both_wavs_because_designs_are_not_reproducible(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        first_audio, first_token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        _freeze(client, character_id="momo", artifact_id=first_token)
        voice_client.audio = b"RIFFvoice-design-b"
        second_audio, second_token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=second_token)

    clip_dir = tmp_path / "voices" / "momo"
    first_name = f"{hashlib.sha256(first_audio).hexdigest()[:16]}.wav"
    second_name = f"{hashlib.sha256(second_audio).hexdigest()[:16]}.wav"
    assert first_name != second_name
    assert (clip_dir / first_name).read_bytes() == first_audio
    assert (clip_dir / second_name).read_bytes() == second_audio
    assert response.json()["ref_audio"] == f"momo/{second_name}"


def test_freeze_unknown_character_is_404_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id="nobody", artifact_id=token)

    assert response.status_code == 404
    assert "nobody" in response.text
    assert _tree(tmp_path) == {"personas/momo/persona.yaml"}


@pytest.mark.parametrize("character_id", ["../../etc", "..\\..\\x", "../momo", "momo/../.."])
def test_freeze_rejects_path_traversal_character_ids(tmp_path, monkeypatch, character_id):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id=character_id, artifact_id=token)

    assert response.status_code == 404
    assert _tree(tmp_path) == {"personas/momo/persona.yaml"}
    assert not (tmp_path / "voices").exists()
    assert not (tmp_path.parent / "etc").exists()
    assert not (tmp_path.parent / "x").exists()
    assert not (tmp_path.parent / "momo").exists()


def test_freeze_never_uses_the_declared_id_as_a_path(tmp_path, monkeypatch):
    """persona.yaml can declare any id, so the id must never be used as a path.

    The write directory comes from the discovered profile's persona_path, and
    the template name falls back to the persona directory name when the id is
    not a legal filename: ``../../escaped`` is a known character (the whitelist
    answers "does this character exist"), which says nothing about whether the
    id is safe to join onto a directory.
    """
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    root = _persona_root(tmp_path)
    declared_id = "../../escaped"
    persona = root / "evil" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text(f"id: {declared_id}\nname: Evil\n", encoding="utf-8")

    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id=declared_id, artifact_id=token)

    assert response.status_code == 200
    digest = hashlib.sha256(audio).hexdigest()[:16]
    body = response.json()
    assert body["character_id"] == declared_id
    assert body["template"] == "evil"
    assert (tmp_path / "voices" / "evil" / f"{digest}.wav").read_bytes() == audio
    assert yaml.safe_load((root / "evil" / "voice.yaml").read_text(encoding="utf-8")) == {
        "template": "evil"
    }
    assert not (tmp_path.parent / "escaped").exists()
    assert not (tmp_path.parent / "escaped.yaml").exists()
    assert not (tmp_path / "escaped").exists()


def test_freeze_unknown_artifact_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        response = _freeze(client, character_id="momo", artifact_id="not-a-real-token")

    assert response.status_code == 404
    assert "generate" in response.text.lower()
    assert not (persona_dir / "voice.yaml").exists()
    assert not (tmp_path / "voices").exists()


def test_artifact_store_evicts_the_oldest_entry_beyond_eight(tmp_path, monkeypatch):
    """Only 8 auditioned designs are kept; evicting must be a clean 404."""
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        tokens = []
        for index in range(9):
            voice_client.audio = f"RIFFvoice-design-{index}".encode()
            _, token = _generate(client, text=f"第{index}句试听。", instruct="年轻女性声线")
            tokens.append(token)

        assert _freeze(client, character_id="momo", artifact_id=tokens[0]).status_code == 404
        assert _freeze(client, character_id="momo", artifact_id=tokens[1]).status_code == 200
        assert _freeze(client, character_id="momo", artifact_id=tokens[8]).status_code == 200


def test_freeze_reload_failure_is_non_fatal_and_files_stay_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(
        tmp_path,
        registry_client=_FakeRegistryClient(error=httpx.ConnectError("connection refused")),
    )
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["activated"] is False
    assert body["reason"]
    assert body["ref_audio"] == f"momo/{hashlib.sha256(audio).hexdigest()[:16]}.wav"
    assert (tmp_path / "voices" / "momo.yaml").is_file()
    assert (persona_dir / "voice.yaml").is_file()


def test_freeze_reload_http_404_is_also_non_fatal(tmp_path, monkeypatch):
    """The reload route may be missing on an older sidecar; every failure is soft."""
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path, registry_client=_FakeRegistryClient(status_code=404))
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    assert response.json()["activated"] is False
    assert response.json()["reason"]


def test_a_broken_sibling_reference_does_not_break_the_freeze(tmp_path, monkeypatch):
    """``shared_with`` reads every sibling's voice.yaml; one bad file must not 500."""

    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo")
    broken = _write_persona(personas, "haru")
    (broken / "voice.yaml").write_text("template: [unclosed\n", encoding="utf-8")

    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    assert response.json()["shared_with"] == []


def test_freeze_request_rejects_extra_fields(tmp_path, monkeypatch):
    """A client must not be able to smuggle text/audio into the frozen profile."""
    monkeypatch.setenv("GSV_TTS_VOICES_ROOT", str(tmp_path / "voices"))
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        response = client.post(
            "/v1/voice-design/freeze",
            json={"character_id": "momo", "artifact_id": token, "text": "smuggled"},
        )
        assert response.status_code == 422

        response = client.post(
            "/v1/voice-design/freeze",
            json={"character_id": "momo", "artifact_id": token, "ref_text": "smuggled"},
        )
        assert response.status_code == 422


# --- character list for the freeze picker -------------------------------------


def test_tts_lab_exposes_the_character_list_the_freeze_picker_reads(tmp_path):
    """The Voice Design panel populates its picker from :9002, not from :8001.

    Without this route the picker renders "角色列表不可用" and freezing is
    impossible from the Workbench -- the whole generate -> freeze loop has no
    way to name a target character.
    """
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/characters")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["characters"]]
    assert "momo" in ids


def test_character_list_does_not_leak_server_side_persona_paths(tmp_path):
    """``persona_path`` is a local filesystem path; the browser has no use for it
    and the freeze flow addresses characters by id."""
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        characters = client.get("/v1/characters").json()["characters"]

    assert characters
    for item in characters:
        assert "persona_path" not in item
        assert item["id"]


def test_character_list_carries_a_display_name(tmp_path):
    """The picker labels options with ``name``; an id-only list would render the
    raw slug for every character."""
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        characters = client.get("/v1/characters").json()["characters"]

    momo = next(item for item in characters if item["id"] == "momo")
    assert momo["name"] == "Momo"
