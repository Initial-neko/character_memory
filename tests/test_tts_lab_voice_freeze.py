"""VoiceDesign audition -> freeze contract.

VoiceDesign is unseeded: four identical requests produced different durations.
Freezing therefore persists the exact WAV bytes that were auditioned, never a
re-synthesis. These tests pin that down against the real filesystem.
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
from character_memory.voices import discover_voice_profiles, load_voice_profile

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


def test_freeze_writes_voice_yaml_with_the_frozen_schema(tmp_path):
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线，清亮柔和。")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    digest = hashlib.sha256(audio).hexdigest()[:16]
    relative = f"voice/{digest}.wav"
    body = response.json()
    assert body == {
        "ok": True,
        "character_id": "momo",
        "voice_id": "momo",
        "ref_audio": relative,
        "ref_text": "你好，这是试听。",
        "activated": True,
        "reason": None,
    }

    document = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
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


def test_frozen_voice_yaml_is_loadable_by_the_registry(tmp_path):
    """The freeze writer and the registry reader must agree on one schema.

    The writer records provenance (``created_at`` / ``instruct`` / ``model``)
    that GSV itself does not need, because a VoiceDesign clip cannot be
    regenerated from its prompt -- the record is the only thing that explains a
    frozen clip later. If the reader refuses those keys, the whole
    generate -> freeze loop still answers 200 and still writes every file, but
    ``activated`` is always False and the voice never reaches the registry: a
    dead end that no single-module test can see.
    """
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=token)
    assert response.status_code == 200

    profile = load_voice_profile(persona_dir / "persona.yaml")

    assert profile is not None
    assert profile.voice_id == "momo"
    assert profile.ref_text == "你好，这是试听。"
    assert Path(profile.ref_audio).is_file()
    assert set(discover_voice_profiles([persona_dir / "persona.yaml"])) == {"momo"}


def test_frozen_voice_yaml_survives_an_unquoted_created_at(tmp_path):
    """``created_at`` is written quoted, but a hand-edited file is not.

    YAML turns an unquoted ISO timestamp into a datetime, so the reader has to
    accept both spellings rather than pin the field to ``str``.
    """
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    (persona_dir / "voice").mkdir()
    (persona_dir / "voice" / "clip.wav").write_bytes(b"RIFFclip")
    (persona_dir / "voice.yaml").write_text(
        "ref_audio: voice/clip.wav\n"
        "ref_text: 你好\n"
        "created_at: 2026-09-19T15:26:34.472897+00:00\n",
        encoding="utf-8",
    )

    profile = load_voice_profile(persona_dir / "persona.yaml")

    assert profile is not None
    assert profile.voice_id == "momo"


def test_frozen_wav_is_exactly_the_auditioned_bytes(tmp_path):
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        audio, token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        assert _freeze(client, character_id="momo", artifact_id=token).status_code == 200
        written = (persona_dir / "voice" / f"{hashlib.sha256(audio).hexdigest()[:16]}.wav").read_bytes()

    assert written == audio
    assert written == b"RIFFvoice-design-a"
    # Exactly one VoiceDesign call: freeze persisted the audition, it did not re-synthesize.
    assert len(voice_client.posts) == 1


def test_ref_text_is_the_text_that_produced_the_audio(tmp_path):
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="第一句试听文本。", instruct="年轻女性声线")
        assert _freeze(client, character_id="momo", artifact_id=token).status_code == 200
        voice_client.audio = b"RIFFvoice-design-b"
        _, second = _generate(client, text="第二句完全不同的文本。", instruct="低沉男声")
        assert _freeze(client, character_id="momo", artifact_id=second).status_code == 200

    document = yaml.safe_load((persona_dir / "voice.yaml").read_text(encoding="utf-8"))
    assert document["ref_text"] == "第二句完全不同的文本。"
    assert document["instruct"] == "低沉男声"


def test_refreeze_keeps_both_wavs_because_designs_are_not_reproducible(tmp_path):
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, voice_client = _build_app(tmp_path)
    with TestClient(app) as client:
        first_audio, first_token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        _freeze(client, character_id="momo", artifact_id=first_token)
        voice_client.audio = b"RIFFvoice-design-b"
        second_audio, second_token = _generate(client, text="你好，这是试听。", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=second_token)

    first_name = f"{hashlib.sha256(first_audio).hexdigest()[:16]}.wav"
    second_name = f"{hashlib.sha256(second_audio).hexdigest()[:16]}.wav"
    assert first_name != second_name
    assert (persona_dir / "voice" / first_name).read_bytes() == first_audio
    assert (persona_dir / "voice" / second_name).read_bytes() == second_audio
    assert response.json()["ref_audio"] == f"voice/{second_name}"


def test_freeze_unknown_character_is_404_and_writes_nothing(tmp_path):
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id="nobody", artifact_id=token)

    assert response.status_code == 404
    assert "nobody" in response.text
    assert _tree(tmp_path) == {"personas/momo/persona.yaml"}


@pytest.mark.parametrize("character_id", ["../../etc", "..\\..\\x", "../momo", "momo/../.."])
def test_freeze_rejects_path_traversal_character_ids(tmp_path, character_id):
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id=character_id, artifact_id=token)

    assert response.status_code == 404
    assert _tree(tmp_path) == {"personas/momo/persona.yaml"}
    assert not (tmp_path / "voice").exists()
    assert not (tmp_path.parent / "etc").exists()
    assert not (tmp_path.parent / "x").exists()
    assert not (tmp_path.parent / "momo").exists()


def test_freeze_writes_beside_the_discovered_persona_even_when_the_id_is_a_path(tmp_path):
    """persona.yaml can declare any id, so the id must never be used as a path.

    The persona directory name is not authoritative either: the write directory
    comes from the discovered profile's persona_path.
    """
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
    assert response.json()["voice_id"] == declared_id
    assert (root / "evil" / "voice" / f"{digest}.wav").read_bytes() == audio
    assert (root / "evil" / "voice.yaml").is_file()
    assert not (tmp_path.parent / "escaped").exists()
    assert not (tmp_path / "escaped").exists()


def test_freeze_unknown_artifact_is_404(tmp_path):
    persona_dir = _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        response = _freeze(client, character_id="momo", artifact_id="not-a-real-token")

    assert response.status_code == 404
    assert "generate" in response.text.lower()
    assert not (persona_dir / "voice.yaml").exists()


def test_artifact_store_evicts_the_oldest_entry_beyond_eight(tmp_path):
    """Only 8 auditioned designs are kept; evicting must be a clean 404."""
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


def test_freeze_reload_failure_is_non_fatal_and_files_stay_on_disk(tmp_path):
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
    assert body["ref_audio"] == f"voice/{hashlib.sha256(audio).hexdigest()[:16]}.wav"
    assert (persona_dir / "voice.yaml").is_file()


def test_freeze_reload_http_404_is_also_non_fatal(tmp_path):
    """The reload route is added by another agent; until then every failure is soft."""
    _write_persona(_persona_root(tmp_path), "momo")
    app, _ = _build_app(tmp_path, registry_client=_FakeRegistryClient(status_code=404))
    with TestClient(app) as client:
        _, token = _generate(client, text="你好", instruct="年轻女性声线")
        response = _freeze(client, character_id="momo", artifact_id=token)

    assert response.status_code == 200
    assert response.json()["activated"] is False
    assert response.json()["reason"]


def test_freeze_request_rejects_extra_fields(tmp_path):
    """A client must not be able to smuggle text/audio into the frozen profile."""
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
