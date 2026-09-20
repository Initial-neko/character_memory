from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import threading
import wave

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from character_memory.gsv_tts_experiment import (
    DEFAULT_SEED,
    GsvRuntimeConfigRequest,
    GsvTtsRequest,
    GsvTtsResult,
    GsvTtsRuntime,
    _env_seed,
    create_gsv_tts_app,
)
from character_memory.voices import VoiceProfile, VoiceProfileError


class RecordingTorch:
    """Stand-in for the torch module. Only ``manual_seed`` is exercised."""

    def __init__(self, on_seed=None):
        self.seeds: list[int] = []
        self._on_seed = on_seed

    def manual_seed(self, value: int) -> None:
        self.seeds.append(int(value))
        if self._on_seed is not None:
            self._on_seed(int(value))


def _gsv_assets(tmp_path: Path) -> tuple[Path, Path, Path]:
    gpt = tmp_path / "voice-e15.ckpt"
    sovits = tmp_path / "voice-e8.pth"
    ref = tmp_path / "reference.wav"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    ref.write_bytes(b"wav")
    return gpt, sovits, ref


def _default_registry(ref: Path) -> dict[str, VoiceProfile]:
    """A registry in which the default voice resolves, so the engine can load.

    Readiness is "the shared models *and* the default template resolve": a
    runtime with no ``murasame`` template reports itself not ready and refuses
    every synthesis. Injected rather than written to disk, because these tests
    are about the seed and the HTTP contract, not about discovery.
    """
    return {
        "murasame": VoiceProfile(
            voice_id="murasame", ref_audio=str(ref), ref_text="参考文本。"
        )
    }


class FakeGsvRuntime:
    preload = False

    def __init__(self):
        self.loaded = False

    def status(self) -> dict:
        return {
            "id": "gsv",
            "ready": True,
            "loaded": self.loaded,
            "model": "fake-gpt.ckpt+fake-sovits.pth",
            "device": "cuda:0",
            "voices": ["murasame"],
            "default_voice": "murasame",
            "supports_speed": True,
            "sample_rate": 32000,
            "reason": None,
        }

    def load(self) -> dict:
        already = self.loaded
        self.loaded = True
        return {**self.status(), "already_loaded": already}

    def configure(self, request: GsvRuntimeConfigRequest) -> dict:
        self.loaded = bool(request.preload)
        return {**self.status(), "changed": ["gpt_model"]}

    def unload(self) -> dict:
        self.loaded = False
        return {**self.status(), "unloaded": True}

    def synthesize(self, request: GsvTtsRequest) -> GsvTtsResult:
        assert request.text == "你好"
        assert request.voice == "murasame"
        assert request.language == "zh"
        assert request.speed == 1.0
        self.loaded = True
        return GsvTtsResult(
            audio=b"RIFFfake-gsv-wav",
            sample_rate=32000,
            model="fake-gpt.ckpt+fake-sovits.pth",
            device="cuda:0",
            voice="murasame",
            inference_ms=612.4,
            audio_ms=2886.0,
            rtf=0.2122,
            cuda_allocated_mb=2100.0,
            cuda_reserved_mb=2300.0,
            cuda_peak_mb=2600.0,
        )


def test_gsv_sidecar_http_contract_without_real_cuda():
    runtime = FakeGsvRuntime()
    app = create_gsv_tts_app(runtime)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["loaded"] is False

        configured = client.post(
            "/v1/configure",
            json={
                "gpt_model": "new.ckpt",
                "sovits_model": "new.pth",
                "ref_audio": "new.wav",
                "ref_text": "参考文本",
                "voice": "murasame",
                "device": "cuda",
                "preload": False,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["loaded"] is False

        loaded = client.post("/v1/load")
        assert loaded.status_code == 200
        assert loaded.json()["loaded"] is True
        assert loaded.json()["already_loaded"] is False

        response = client.post(
            "/v1/tts",
            json={"text": "你好", "voice": "murasame", "language": "zh", "speed": 1.0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["x-tts-provider"] == "gsv"
        assert response.headers["x-tts-voice"] == "murasame"
        assert response.headers["x-tts-device"] == "cuda:0"
        assert response.headers["x-tts-inference-ms"] == "612.4"
        assert response.headers["x-tts-rtf"] == "0.2122"
        assert response.headers["x-tts-cuda-peak-mb"] == "2600.0"
        assert response.content == b"RIFFfake-gsv-wav"


def test_gsv_runtime_uses_upstream_infer_batched_contract(tmp_path):
    gpt = tmp_path / "voice-e15.ckpt"
    sovits = tmp_path / "voice-e8.pth"
    ref = tmp_path / "reference.wav"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    ref.write_bytes(b"wav")

    calls = {}

    class FakeTts:
        def __init__(self, **kwargs):
            calls["init"] = kwargs
            self.tts_config = SimpleNamespace(device="cuda:0")

        def load_gpt_model(self, path):
            calls["gpt"] = path

        def load_sovits_model(self, path):
            calls["sovits"] = path

        def cache_spk_audio(self, path, **kwargs):
            calls["cache_spk"] = (path, kwargs)

        def cache_prompt_audio(self, **kwargs):
            calls["cache_prompt"] = kwargs

        def infer_batched(self, **kwargs):
            calls["infer"] = kwargs
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25, -0.25, 0.5], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cuda:0",
        default_voice="murasame",
        tts_factory=FakeTts,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        # Readiness needs the default template to resolve, and the calls below
        # assert on the global reference this test wrote. An injected registry is
        # authoritative and keeps both exactly where the test put them.
        voices=_default_registry(ref),
    )

    status = runtime.status()
    assert status["ready"] is True
    assert status["loaded"] is False

    result = runtime.synthesize(
        GsvTtsRequest(text="你好", voice="murasame", language="zh", speed=1.2)
    )

    assert calls["init"]["gpt_cache"] == [(1, 512), (1, 1024), (4, 512), (4, 1024)]
    assert calls["init"]["sovits_cache"] == [50, 55]
    assert calls["init"]["use_bert"] is True
    assert calls["init"]["use_flash_attn"] is False
    assert calls["gpt"] == str(gpt)
    assert calls["sovits"] == str(sovits)
    assert calls["cache_spk"][0] == str(ref)
    assert calls["cache_prompt"]["prompt_audio_paths"] == str(ref)
    assert calls["infer"]["spk_audio_paths"] == str(ref)
    assert calls["infer"]["prompt_audio_paths"] == str(ref)
    assert calls["infer"]["prompt_audio_texts"] == "参考文本。"
    assert calls["infer"]["texts"] == "你好"
    assert calls["infer"]["text_languages"] == "zh"
    assert calls["infer"]["speed"] == 1.2
    assert calls["infer"]["gpt_model"] == str(gpt)
    assert calls["infer"]["sovits_model"] == str(sovits)

    assert result.sample_rate == 32000
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 32000
        assert wav.getnframes() == 4


def test_gsv_runtime_can_hot_configure_and_unload(tmp_path):
    gpt = tmp_path / "voice.ckpt"
    sovits = tmp_path / "voice.pth"
    ref = tmp_path / "ref.wav"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    ref.write_bytes(b"wav")

    class FakeTts:
        def __init__(self, **kwargs):
            self.tts_config = SimpleNamespace(device="cuda:0")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

    runtime = GsvTtsRuntime(
        gpt_model="",
        sovits_model="",
        ref_audio="",
        ref_text="",
        device="cuda",
        default_voice="murasame",
        tts_factory=FakeTts,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        # ``configure`` re-reads no voice files, so readiness after it has to
        # come from the registry the runtime was built with.
        voices=_default_registry(ref),
    )
    assert runtime.status()["ready"] is False

    configured = runtime.configure(
        GsvRuntimeConfigRequest(
            gpt_model=str(gpt),
            sovits_model=str(sovits),
            ref_audio=str(ref),
            ref_text="参考文本",
            voice="murasame",
            device="cuda",
            preload=True,
        )
    )
    assert configured["ready"] is True
    assert configured["loaded"] is True
    assert Path(runtime.gpt_model) == gpt
    assert runtime.ref_text == "参考文本"

    unloaded = runtime.unload()
    assert unloaded["ready"] is True
    assert unloaded["loaded"] is False


def test_gsv_status_reports_missing_assets_without_importing_gsv(tmp_path):
    runtime = GsvTtsRuntime(
        gpt_model="",
        sovits_model="",
        ref_audio="",
        ref_text="",
        tts_factory=lambda **kwargs: None,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
    )
    status = runtime.status()
    assert status["ready"] is False
    assert "Missing GSV configuration" in status["reason"]


def test_gsv_seed_is_applied_under_the_lock_and_before_inference(tmp_path):
    """Voice stability depends on this ordering, not just on the seed existing.

    GSV-TTS-Lite reads PyTorch's *global* RNG and takes no ``generator=``, so a
    seed set outside the lock can be consumed by a concurrent request before
    ``infer_batched`` runs. That would silently reintroduce the drift the seed
    is there to remove, and only under load -- the hardest case to notice.
    """
    gpt, sovits, ref = _gsv_assets(tmp_path)
    holder: dict = {}
    observed: dict = {}

    def probe(value: int) -> None:
        acquired: list[bool] = []
        thread = threading.Thread(
            target=lambda: acquired.append(holder["runtime"]._lock.acquire(blocking=False))
        )
        thread.start()
        thread.join()
        if acquired[0]:
            holder["runtime"]._lock.release()
        observed["seed"] = value
        observed["lock_held"] = not acquired[0]

    class FakeTts:
        def __init__(self, **_kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

        def infer_batched(self, **_kwargs):
            # The seed must already be set by the time inference starts.
            observed["seed_at_infer"] = observed.get("seed")
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cpu",
        default_voice="murasame",
        tts_factory=FakeTts,
        torch_module=RecordingTorch(on_seed=probe),
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        voices=_default_registry(ref),
    )
    holder["runtime"] = runtime

    result = runtime.synthesize(GsvTtsRequest(text="你好"))

    assert observed["seed"] == DEFAULT_SEED
    assert observed["lock_held"] is True, "manual_seed ran outside the RLock"
    assert observed["seed_at_infer"] == DEFAULT_SEED, "inference started before seeding"
    assert result.seed == DEFAULT_SEED


def test_gsv_request_seed_overrides_the_runtime_default(tmp_path):
    gpt, sovits, ref = _gsv_assets(tmp_path)
    torch = RecordingTorch()

    class FakeTts:
        def __init__(self, **_kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

        def infer_batched(self, **_kwargs):
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cpu",
        tts_factory=FakeTts,
        torch_module=torch,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        voices=_default_registry(ref),
    )

    result = runtime.synthesize(GsvTtsRequest(text="你好", seed=7777))

    assert torch.seeds == [7777]
    assert result.seed == 7777


def test_gsv_seed_none_keeps_the_unseeded_behaviour(tmp_path):
    gpt, sovits, ref = _gsv_assets(tmp_path)
    torch = RecordingTorch()

    class FakeTts:
        def __init__(self, **_kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

        def infer_batched(self, **_kwargs):
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cpu",
        seed=None,
        tts_factory=FakeTts,
        torch_module=torch,
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        voices=_default_registry(ref),
    )

    result = runtime.synthesize(GsvTtsRequest(text="你好"))

    assert torch.seeds == []
    assert result.seed is None
    assert runtime.status()["seed"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_SEED),
        ("", None),
        ("  none ", None),
        ("off", None),
        ("random", None),
        ("-1", None),
        ("1234", 1234),
        (" 7777 ", 7777),
        ("0", 0),
    ],
)
def test_gsv_env_seed_parsing(raw, expected):
    assert _env_seed(raw) == expected


def test_gsv_env_seed_rejects_garbage():
    with pytest.raises(ValueError):
        _env_seed("murasame")


def test_gsv_reports_the_seed_it_used_over_http(tmp_path):
    gpt, sovits, ref = _gsv_assets(tmp_path)

    class FakeTts:
        def __init__(self, **_kwargs):
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, _path):
            pass

        def load_sovits_model(self, _path):
            pass

        def cache_spk_audio(self, _path, **_kwargs):
            pass

        def cache_prompt_audio(self, **_kwargs):
            pass

        def infer_batched(self, **_kwargs):
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    runtime = GsvTtsRuntime(
        gpt_model=str(gpt),
        sovits_model=str(sovits),
        ref_audio=str(ref),
        ref_text="参考文本。",
        device="cpu",
        tts_factory=FakeTts,
        torch_module=RecordingTorch(),
        persona_root=tmp_path / "personas",
        voices_root=tmp_path / "voices",
        voices=_default_registry(ref),
    )
    with TestClient(create_gsv_tts_app(runtime)) as client:
        seeded = client.post("/v1/tts", json={"text": "你好", "seed": 4242})
        assert seeded.status_code == 200
        assert seeded.headers["x-tts-seed"] == "4242"

        default = client.post("/v1/tts", json={"text": "你好"})
        assert default.headers["x-tts-seed"] == str(DEFAULT_SEED)

        health = client.get("/health").json()
        assert health["supports_seed"] is True
        assert health["seed"] == DEFAULT_SEED


# --- Per-persona voice registry -------------------------------------------------


def _persona_root(tmp_path: Path) -> Path:
    return tmp_path / "personas"


def _voices_root(tmp_path: Path) -> Path:
    """The templates root ``_Rig`` pins by default: a sibling of ``personas/``."""

    return tmp_path / "voices"


def _write_template(
    tmp_path: Path, name: str, *, ref_text: str = "默认参考文本。"
) -> str:
    """Register a template directly under ``voices/`` and return its clip path.

    Readiness is "the shared models *and* the default template resolve", and
    every synthesis loads the engine, so a rig that will be asked to speak needs
    one. Written as a template rather than a persona: templates are the only
    thing that owns a reference clip now, and a persona would register a
    character the test did not ask for.
    """
    directory = _voices_root(tmp_path) / name
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / "template-clip.wav"
    clip.write_bytes(b"RIFFfake-voice-reference")
    (_voices_root(tmp_path) / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"ref_audio": f"{name}/{clip.name}", "ref_text": ref_text},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return str(clip.resolve())


def _write_persona(
    root: Path,
    character_id: str,
    *,
    ref_audio: str | None = None,
    ref_text: str = "参考文本。",
    gpt_model: str | None = None,
    sovits_model: str | None = None,
    template: str | None = None,
) -> Path:
    """Write a real ``personas/<id>/`` tree and the template it names.

    A voice is two files now: the clip lives in a template under ``voices/`` and
    the character's ``voice.yaml`` only names it. The template is keyed on the
    character id so that ``voice: <id>`` is the single key these tests exercise,
    and the clip sits in the template's own directory, referenced *relatively*
    -- the shape the freeze route writes. Passing ``ref_audio=None`` writes a
    persona without a ``voice.yaml`` (the normal case for most characters) and
    therefore no template either.

    ``template=`` points the character at a template that already exists instead
    of writing one of its own, which is the only shape where the character id
    and the template name differ.

    The templates root is ``root``'s sibling, which is what ``_Rig`` pins as its
    default ``voices_root``.
    """
    directory = root / character_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "persona.yaml").write_text(
        f"id: {character_id}\nname: {character_id}\n", encoding="utf-8"
    )
    if template is not None:
        (directory / "voice.yaml").write_text(
            yaml.safe_dump({"template": template}, sort_keys=False), encoding="utf-8"
        )
        return directory
    if ref_audio is None:
        return directory

    templates = root.parent / "voices"
    (templates / character_id).mkdir(parents=True, exist_ok=True)
    (templates / character_id / ref_audio).write_bytes(b"RIFFfake-voice-reference")
    document: dict[str, object] = {
        "ref_audio": f"{character_id}/{ref_audio}",
        "ref_text": ref_text,
    }
    if gpt_model is not None:
        document["gpt_model"] = gpt_model
    if sovits_model is not None:
        document["sovits_model"] = sovits_model
    (templates / f"{character_id}.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (directory / "voice.yaml").write_text(
        yaml.safe_dump({"template": character_id}, sort_keys=False), encoding="utf-8"
    )
    return directory


def _recording_engine(calls: dict[str, list]):
    """Fake GSV engine that records every call the runtime makes on it."""

    class FakeTts:
        def __init__(self, **kwargs):
            calls.setdefault("init", []).append(kwargs)
            self.tts_config = SimpleNamespace(device="cpu")

        def load_gpt_model(self, path):
            calls.setdefault("gpt", []).append(path)

        def load_sovits_model(self, path):
            calls.setdefault("sovits", []).append(path)

        def cache_spk_audio(self, path, **kwargs):
            calls.setdefault("cache_spk", []).append((path, kwargs))

        def cache_prompt_audio(self, **kwargs):
            calls.setdefault("cache_prompt", []).append(kwargs)

        def infer_batched(self, **kwargs):
            calls.setdefault("infer", []).append(kwargs)
            return (
                SimpleNamespace(
                    audio_data=np.asarray([0.0, 0.25], dtype=np.float32),
                    samplerate=32000,
                ),
            )

    return FakeTts


class _Rig:
    """A runtime wired to a recording fake engine, plus its global asset paths."""

    def __init__(self, tmp_path: Path, **overrides):
        self.calls: dict[str, list] = {}
        self.gpt = tmp_path / "voice-e15.ckpt"
        self.sovits = tmp_path / "voice-e8.pth"
        self.ref = tmp_path / "global-reference.wav"
        self.gpt.write_bytes(b"gpt")
        self.sovits.write_bytes(b"sovits")
        self.ref.write_bytes(b"wav")
        kwargs = {
            "gpt_model": str(self.gpt),
            "sovits_model": str(self.sovits),
            "ref_audio": str(self.ref),
            "ref_text": "全局参考文本。",
            "device": "cpu",
            "default_voice": "murasame",
            "tts_factory": _recording_engine(self.calls),
            # The voices root is pinned to the tmp tree because the module
            # default ("voices") is *relative*: it resolves against the cwd --
            # the repo root under pytest. A test that inherited it would read
            # the developer's real tree instead of the one it just wrote, which
            # is order-dependent on their data, and one that writes templates
            # would write them into their workspace.
            #
            # ``persona_root`` is deliberately *not* defaulted here: the caller
            # passes the tree it wrote, and the one test that means to read
            # ``GSV_TTS_PERSONA_ROOT`` instead must reach the environment.
            "voices_root": _voices_root(tmp_path),
        }
        kwargs.update(overrides)
        self.runtime = GsvTtsRuntime(**kwargs)

    @property
    def last_infer(self) -> dict:
        return self.calls["infer"][-1]

    def synthesize(self, **request) -> tuple[GsvTtsResult, dict]:
        """Synthesize one line and return the result plus the engine-call kwargs."""
        result = self.runtime.synthesize(GsvTtsRequest(text="你好", **request))
        return result, self.last_infer


def test_gsv_synthesize_clones_the_requested_personas_reference_audio(tmp_path):
    """A registered voice_id must reach GSV as *that persona's* reference clip.

    Browser clients send ``voice: <character id>`` for every character, so this
    is the only path that turns the registry into audible per-character voices.
    """
    personas = _persona_root(tmp_path)
    _write_template(tmp_path, "murasame")
    _write_persona(
        personas, "momo", ref_audio="momo-ref.wav", ref_text="桃子的参考文本。"
    )
    rig = _Rig(tmp_path, persona_root=personas)

    result, infer = rig.synthesize(voice="momo")

    expected = str((_voices_root(tmp_path) / "momo" / "momo-ref.wav").resolve())
    assert infer["spk_audio_paths"] == expected
    assert infer["prompt_audio_paths"] == expected
    assert infer["prompt_audio_texts"] == "桃子的参考文本。"
    assert result.voice == "momo"


def test_gsv_synthesize_falls_back_to_the_default_voice_for_unknown_ids(tmp_path):
    """An unregistered id must degrade, not reject.

    Most characters have no ``voice.yaml`` yet. Raising here would mute every
    character that has not been given a voice instead of letting it speak in the
    default voice -- which is the default *template*, since that is where the
    reference clip lives now.
    """
    personas = _persona_root(tmp_path)
    default_clip = _write_template(tmp_path, "murasame")
    _write_persona(personas, "momo")  # persona.yaml only, no voice yet
    rig = _Rig(tmp_path, persona_root=personas)

    result, infer = rig.synthesize(voice="momo")

    assert infer["spk_audio_paths"] == default_clip
    assert infer["prompt_audio_texts"] == "默认参考文本。"
    assert result.voice == "murasame"

    with TestClient(create_gsv_tts_app(rig.runtime)) as client:
        response = client.post("/v1/tts", json={"text": "你好", "voice": "does-not-exist"})

    assert response.status_code == 200
    # The header must name the voice actually used, not the one requested.
    assert response.headers["x-tts-voice"] == "murasame"
    assert rig.last_infer["spk_audio_paths"] == default_clip


@pytest.mark.parametrize("payload", [{}, {"voice": ""}, {"voice": "   "}])
def test_gsv_synthesize_uses_the_runtime_default_when_no_voice_is_requested(
    tmp_path, payload
):
    personas = _persona_root(tmp_path)
    default_clip = _write_template(tmp_path, "murasame")
    _write_persona(personas, "momo", ref_audio="momo.wav", ref_text="桃子。")
    rig = _Rig(tmp_path, persona_root=personas)

    with TestClient(create_gsv_tts_app(rig.runtime)) as client:
        response = client.post("/v1/tts", json={"text": "你好", **payload})

    assert response.status_code == 200
    assert response.headers["x-tts-voice"] == "murasame"
    assert rig.last_infer["spk_audio_paths"] == default_clip
    assert rig.last_infer["prompt_audio_texts"] == "默认参考文本。"


def test_gsv_voice_profile_overrides_models_only_when_it_pins_them(tmp_path):
    """Unset profile models inherit the runtime globals; set ones win."""
    personas = _persona_root(tmp_path)
    _write_template(tmp_path, "murasame")
    _write_persona(personas, "momo", ref_audio="momo.wav", ref_text="桃子。")
    _write_persona(
        personas,
        "rin",
        ref_audio="rin.wav",
        ref_text="凛。",
        gpt_model="rin-gpt.ckpt",
        sovits_model="rin-sovits.pth",
    )
    rig = _Rig(tmp_path, persona_root=personas)

    _, inherited = rig.synthesize(voice="momo")
    assert inherited["gpt_model"] == str(rig.gpt)
    assert inherited["sovits_model"] == str(rig.sovits)

    _, pinned = rig.synthesize(voice="rin")
    assert pinned["gpt_model"] == "rin-gpt.ckpt"
    assert pinned["sovits_model"] == "rin-sovits.pth"


def test_gsv_runtime_without_a_default_template_refuses_the_request(tmp_path):
    """A missing personas directory is not an error: the sidecar still starts.

    It cannot synthesize -- readiness is "the default template resolves", and
    nothing here defines one -- and that arrives as a reported reason rather
    than a crash at startup. A request against it must then fail loudly. The
    runtime-global ``ref_audio``/``ref_text`` pair used to answer this corner,
    and it is reachable whenever the engine is already warm (``load()``
    short-circuits on ``already_loaded``), so the "it will not be asked to
    synthesize" reasoning behind that fallback was false: the operator got HTTP
    200, the wrong voice, and a ``/health`` that said not ready.
    """
    rig = _Rig(tmp_path, persona_root=tmp_path / "does-not-exist")

    status = rig.runtime.status()
    assert status["ready"] is False
    assert "murasame" in status["reason"]
    assert status["voices"] == ["murasame"]

    with TestClient(create_gsv_tts_app(rig.runtime)) as client:
        response = client.post("/v1/tts", json={"text": "你好", "voice": "momo"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "momo" in detail, detail
    assert "murasame" in detail, detail


def test_gsv_synthesize_refuses_after_the_default_template_clip_disappears(tmp_path):
    """The ``already_loaded`` early return must not bypass readiness.

    The engine stays warm across a registry change -- that is the point of
    ``reload`` -- while the template it was warmed for can be renamed, or its
    clip deleted. Checking readiness only on the cold path answered those
    requests from a reference that no longer resolves, with ``/health`` already
    reporting not ready.
    """
    personas = _persona_root(tmp_path)
    clip = _write_template(tmp_path, "murasame")
    rig = _Rig(tmp_path, persona_root=personas)

    result, _ = rig.synthesize(voice="murasame")
    assert result.voice == "murasame"
    assert rig.runtime.status()["loaded"] is True

    Path(clip).unlink()

    with pytest.raises(RuntimeError, match="murasame"):
        rig.runtime.synthesize(GsvTtsRequest(text="你好", voice="murasame"))

    assert rig.runtime.status()["ready"] is False


def test_gsv_runtime_refuses_to_start_on_a_broken_voice_asset(tmp_path):
    """A template that exists but is unusable is a config error, not a skip.

    GSV cannot recover from an empty reference transcript at synthesis time
    (``cache_prompt_audio`` raises), so the only place an operator can see the
    mistake is at load. Swallowing it would surface as a failure per utterance.
    """
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo", ref_audio="momo.wav", ref_text="   ")

    with pytest.raises(VoiceProfileError) as excinfo:
        _Rig(tmp_path, persona_root=personas)

    assert str(_voices_root(tmp_path) / "momo.yaml") in str(excinfo.value)


def test_gsv_runtime_skips_personas_that_have_no_voice_profile(tmp_path):
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo")  # no voice.yaml at all
    _write_persona(personas, "rin", ref_audio="rin.wav", ref_text="凛。")

    rig = _Rig(tmp_path, persona_root=personas)

    voices = rig.runtime.status()["voices"]
    assert "rin" in voices
    assert "momo" not in voices


def test_gsv_status_lists_every_registered_voice_and_always_the_default(tmp_path):
    """Settings Center builds its voice dropdown from this list (zero UI work)."""
    personas = _persona_root(tmp_path)
    _write_persona(personas, "momo", ref_audio="momo.wav", ref_text="桃子。")
    _write_persona(personas, "rin", ref_audio="rin.wav", ref_text="凛。")

    unregistered_default = _Rig(tmp_path, persona_root=personas)
    assert unregistered_default.runtime.status()["voices"] == ["momo", "rin", "murasame"]
    assert unregistered_default.runtime.status()["default_voice"] == "murasame"

    registered_default = _Rig(tmp_path, persona_root=personas, default_voice="momo")
    assert registered_default.runtime.status()["voices"].count("momo") == 1


def test_gsv_status_offers_template_names_not_character_ids(tmp_path):
    """``GSV_TTS_VOICE`` must name a template, so the selector must list only those.

    A character id is a perfectly good *request* voice (the browser sends one for
    every character), but it is not a valid value for the setting -- and since a
    character can shadow a template of the same name in the merged registry,
    offering ids here would let the two mean different things. Spec §6.2.
    """
    personas = _persona_root(tmp_path)
    default_clip = _write_template(tmp_path, "murasame")
    _write_persona(personas, "momo", template="murasame")

    rig = _Rig(tmp_path, persona_root=personas)

    assert rig.runtime.status()["voices"] == ["murasame"]

    # ... while the character id still resolves on the request path.
    result, infer = rig.synthesize(voice="momo")
    assert result.voice == "momo"
    assert infer["spk_audio_paths"] == default_clip


def test_gsv_voice_reload_adds_profiles_without_rebuilding_the_engine(tmp_path):
    """Reload re-reads files; it must not touch VRAM.

    Rebuilding the engine would unload the GPT/SoVITS weights and drop the warm
    speaker cache -- minutes of GPU work -- for a change that only adds a
    reference clip. New reference audio is cached lazily by ``infer_batched``.
    """
    personas = _persona_root(tmp_path)
    _write_template(tmp_path, "murasame")
    _write_persona(personas, "momo")
    rig = _Rig(tmp_path, persona_root=personas)

    with TestClient(create_gsv_tts_app(rig.runtime)) as client:
        assert client.post("/v1/tts", json={"text": "你好"}).status_code == 200
        assert len(rig.calls["init"]) == 1

        _write_persona(personas, "rin", ref_audio="rin.wav", ref_text="凛。")
        reloaded = client.post("/v1/voices/reload")

        assert reloaded.status_code == 200
        assert "rin" in reloaded.json()["voices"]
        assert reloaded.json()["loaded"] is True

        response = client.post("/v1/tts", json={"text": "你好", "voice": "rin"})

    assert response.status_code == 200
    assert response.headers["x-tts-voice"] == "rin"
    assert rig.last_infer["spk_audio_paths"] == str(
        (_voices_root(tmp_path) / "rin" / "rin.wav").resolve()
    )
    assert len(rig.calls["init"]) == 1, "reload rebuilt the engine"
    assert len(rig.calls["gpt"]) == 1, "reload reloaded the GPT model"
    assert len(rig.calls["sovits"]) == 1, "reload reloaded the SoVITS model"


def test_gsv_configure_request_model_does_not_accept_a_voices_field():
    """Manifests change only through /v1/voices/reload.

    ``configure()`` unloads the engine for any tracked field that changes, so
    accepting ``voices`` here would drop the warm models for a pure file change.
    """
    assert "voices" not in GsvRuntimeConfigRequest.model_fields
    with pytest.raises(ValidationError):
        GsvRuntimeConfigRequest.model_validate({"voices": ["momo"]})

    with TestClient(create_gsv_tts_app(FakeGsvRuntime())) as client:
        response = client.post("/v1/configure", json={"voices": ["momo"]})

    assert response.status_code == 422


def test_gsv_runtime_reads_the_persona_root_from_the_environment(tmp_path, monkeypatch):
    """The stack and start script point the sidecar at an absolute personas dir.

    The directory the env names is deliberately *not* ``_Rig``'s own: if the rig
    supplied a persona root of its own, this would hold whether or not the
    environment was ever consulted, which is exactly how the env branch lost its
    coverage once already.
    """
    personas = tmp_path / "env-personas"
    _write_persona(personas, "momo", ref_audio="momo.wav", ref_text="桃子。")
    monkeypatch.setenv("GSV_TTS_PERSONA_ROOT", str(personas))

    rig = _Rig(tmp_path)

    assert rig.runtime.persona_root == str(personas)
    assert "momo" in rig.runtime.status()["voices"]
