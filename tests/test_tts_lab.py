from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote

from fastapi.testclient import TestClient

from character_memory.tts_lab import EdgeTtsProvider, GsvSidecarProvider, LabSynthesisResult, Qwen3VoiceDesignSidecar, SherpaMediaProvider, TtsLabRuntime, create_tts_lab_app


class FakeTtsProvider:
    def status(self) -> dict:
        return {
            "id": "fake",
            "label": "Fake TTS",
            "ready": True,
            "loaded": True,
            "voices": ["中文女"],
            "default_voice": "中文女",
            "supports_speed": True,
            "model": "fake-model",
            "device": "cpu",
            "reason": None,
        }

    def synthesize(self, text: str, *, voice: str, speed: float) -> LabSynthesisResult:
        assert text == "你好"
        assert voice == "中文女"
        assert speed == 1.0
        return LabSynthesisResult(
            audio=b"RIFFfake-wav",
            sample_rate=24000,
            provider="fake",
            voice=voice,
            model="fake-model",
            device="cpu",
            inference_ms=12.3,
            audio_ms=456.7,
        )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _FakeResponse(self.payload)



class _FakeQwenResponse:
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


class _FakeGsvClient:
    def __init__(self):
        self.posts = []

    def get(self, *args, **kwargs):
        return _FakeQwenResponse(
            {
                "ready": True,
                "loaded": True,
                "voices": ["murasame"],
                "default_voice": "murasame",
                "model": "Murasame-e15.ckpt+Murasame_e8_s192.pth",
                "device": "cuda:0",
                "reason": None,
            }
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeQwenResponse(
            content=b"RIFFgsv",
            headers={
                "x-tts-voice": "murasame",
                "x-tts-model": "Murasame-e15.ckpt%2BMurasame_e8_s192.pth",
                "x-tts-device": "cuda:0",
                "x-tts-inference-ms": "610.0",
                "x-tts-audio-ms": "2500",
                "x-tts-sample-rate": "32000",
            },
        )


class _FakeVoiceDesignClient:
    def __init__(self):
        self.posts = []

    def get(self, url, **kwargs):
        assert url == "http://127.0.0.1:9015/health"
        return _FakeQwenResponse(
            {
                "ready": True,
                "loaded": True,
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                "device": "cuda:0",
                "reason": None,
            }
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeQwenResponse(
            content=b"RIFFvoice-design",
            headers={
                "content-type": "audio/wav",
                "x-voice-design-model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                "x-voice-design-device": "cuda:0",
                "x-voice-design-inference-ms": "3210.5",
                "x-voice-design-audio-ms": "2800",
                "x-voice-design-sample-rate": "24000",
            },
        )


class _FakePolishModel:
    model = "fake-llm"

    def __init__(self):
        self.calls = []

    def complete_text_for_session(self, messages, session_id):
        self.calls.append((messages, session_id, {}))
        return "年轻女性声线，音色清亮柔和，略带慵懒感，语速中等偏慢，避免刻意撒娇。"

    def close(self):
        pass


def test_tts_lab_provider_status_and_synthesis_contract():
    runtime = TtsLabRuntime({"fake": FakeTtsProvider()})
    app = create_tts_lab_app(runtime)
    with TestClient(app) as client:
        providers = client.get("/v1/providers")
        assert providers.status_code == 200
        assert providers.json()["providers"][0]["id"] == "fake"

        provider = client.get("/v1/providers/fake")
        assert provider.status_code == 200
        assert provider.json()["provider"]["id"] == "fake"
        assert provider.json()["provider"]["ready"] is True

        response = client.post(
            "/v1/tts",
            json={"provider": "fake", "text": "你好", "voice": "中文女", "speed": 1.0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content == b"RIFFfake-wav"
        assert response.headers["x-tts-provider"] == "fake"
        assert unquote(response.headers["x-tts-voice"]) == "中文女"
        assert response.headers["x-tts-sample-rate"] == "24000"
        assert response.headers["x-tts-inference-ms"] == "12.3"



def test_gsv_sidecar_provider_status_and_synthesis():
    client = _FakeGsvClient()
    provider = GsvSidecarProvider(client=client)

    status = provider.status()
    assert status["ready"] is True
    assert status["loaded"] is True
    assert status["default_voice"] == "murasame"
    assert status["device"] == "cuda:0"

    result = provider.synthesize("你好", voice="murasame", speed=1.0)
    assert result.audio == b"RIFFgsv"
    assert result.provider == "gsv"
    assert result.voice == "murasame"
    assert result.device == "cuda:0"
    assert result.sample_rate == 32000
    assert result.inference_ms == 610.0
    assert client.posts == [
        (
            "http://127.0.0.1:9014/v1/tts",
            {
                "json": {
                    "text": "你好",
                    "voice": "murasame",
                    "language": "zh",
                    "speed": 1.0,
                },
                "timeout": 180.0,
            },
        )
    ]


def test_qwen3_voice_design_sidecar_contract():
    client = _FakeVoiceDesignClient()
    tool = Qwen3VoiceDesignSidecar(client=client)

    status = tool.status()
    assert status["ready"] is True
    assert status["loaded"] is True
    assert status["model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

    result = tool.generate(
        "你好，这是试听。",
        language="Chinese",
        instruct="年轻女性声线，清亮柔和。",
        max_new_tokens=2048,
    )
    assert result.audio == b"RIFFvoice-design"
    assert result.provider == "qwen3-voice-design"
    assert result.device == "cuda:0"
    assert result.sample_rate == 24000
    assert client.posts == [
        (
            "http://127.0.0.1:9015/v1/voice-design",
            {
                "json": {
                    "text": "你好，这是试听。",
                    "language": "Chinese",
                    "instruct": "年轻女性声线，清亮柔和。",
                    "max_new_tokens": 2048,
                },
                "timeout": 300.0,
            },
        )
    ]


def test_voice_design_workbench_status_generate_and_standard_llm_polish():
    vd_client = _FakeVoiceDesignClient()
    voice_design = Qwen3VoiceDesignSidecar(client=vd_client)
    polish_model = _FakePolishModel()
    app = create_tts_lab_app(
        TtsLabRuntime({"fake": FakeTtsProvider()}),
        voice_design=voice_design,
        model_factory=lambda: polish_model,
    )

    with TestClient(app) as client:
        status = client.get("/v1/voice-design/status")
        assert status.status_code == 200
        assert status.json()["voice_design"]["ready"] is True

        polished = client.post(
            "/v1/voice-design/polish",
            json={"description": "年轻一点，温柔，不要太嗲", "language": "Chinese"},
        )
        assert polished.status_code == 200
        assert "清亮柔和" in polished.json()["instruct"]
        assert polished.json()["model"] == "fake-llm"

        generated = client.post(
            "/v1/voice-design/generate",
            json={
                "text": "你好，这是试听。",
                "language": "Chinese",
                "instruct": polished.json()["instruct"],
                "max_new_tokens": 2048,
            },
        )
        assert generated.status_code == 200
        assert generated.content == b"RIFFvoice-design"
        assert generated.headers["x-voice-design-device"] == "cuda:0"
        assert unquote(generated.headers["x-voice-design-model"]) == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

    assert polish_model.calls[0][1] == "tts-voice-design-polish"


def test_edge_tts_provider_streams_mp3_without_network_dependency_in_test():
    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice
            captured["kwargs"] = kwargs

        def stream_sync(self):
            yield {"type": "audio", "data": b"a" * 3000}
            yield {"type": "SentenceBoundary", "offset": 0, "duration": 1}
            yield {"type": "audio", "data": b"b" * 3000}

    fake_edge = type("FakeEdge", (), {"Communicate": FakeCommunicate})
    provider = EdgeTtsProvider(edge_module=fake_edge)
    status = provider.status()
    assert status["ready"] is True
    assert status["network_required"] is True
    assert status["default_voice"] == "zh-CN-XiaoxiaoNeural"

    result = provider.synthesize("你好", voice="zh-CN-XiaoxiaoNeural", speed=1.2)
    assert result.audio == b"a" * 3000 + b"b" * 3000
    assert result.media_type == "audio/mpeg"
    assert result.sample_rate == 24000
    assert result.audio_ms == 1000.0
    assert result.provider == "edge"
    assert result.device == "cloud"
    assert captured["kwargs"]["rate"] == "+20%"
    assert captured["kwargs"]["volume"] == "+0%"
    assert captured["kwargs"]["pitch"] == "+0Hz"
    assert captured["kwargs"]["connect_timeout"] == 10
    assert captured["kwargs"]["receive_timeout"] == 30


def test_tts_lab_unknown_provider_is_a_client_error():
    app = create_tts_lab_app(TtsLabRuntime({"fake": FakeTtsProvider()}))
    with TestClient(app) as client:
        response = client.post(
            "/v1/tts",
            json={"provider": "missing", "text": "你好", "voice": "中文女", "speed": 1.0},
        )
        assert response.status_code == 400
        assert "Unknown TTS provider" in response.text

        status = client.get("/v1/providers/missing")
        assert status.status_code == 404
        assert "Unknown TTS provider" in status.text


def test_sherpa_lab_status_reads_underlying_runtime_when_formal_tts_is_other_provider():
    client = _FakeClient(
        {
            "tts": {"ready": True, "provider": "kokoro", "model": "kokoro"},
            "tts_runtime": {
                "ready": False,
                "loaded": False,
                "provider": "sherpa-vits",
                "model": "sherpa.onnx",
                "device": "cpu",
                "reason": "missing sherpa assets",
            },
        }
    )
    status = SherpaMediaProvider(client=client).status()
    assert status["ready"] is False
    assert status["model"] == "sherpa.onnx"
    assert status["reason"] == "missing sherpa assets"


def test_tts_lab_static_provider_inventory_and_dependency_isolation():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    server = Path("src/character_memory/tts_lab.py").read_text(encoding="utf-8")
    sidecar = Path("scripts/cosyvoice_sidecar.py").read_text(encoding="utf-8")
    setup_media = Path("scripts/setup-media-models.sh").read_text(encoding="utf-8")
    setup_tts = Path("scripts/setup-tts-models.sh").read_text(encoding="utf-8")
    prefetch = Path("scripts/prefetch_tts_models.py").read_text(encoding="utf-8")

    assert 'tts-kokoro = [' in pyproject
    assert '"kokoro>=0.9.4,<1"' in pyproject
    assert '"misaki[zh]>=0.9.4,<1"' in pyproject
    assert '"huggingface-hub>=0.30,<2"' in pyproject
    assert 'character-tts-lab = "character_memory.tts_lab:main"' in pyproject
    all_extra = pyproject.split("all = [", 1)[1].split("\n]\n", 1)[0]
    assert '"kokoro>=0.9.4,<1"' in all_extra
    assert '"misaki[zh]>=0.9.4,<1"' in all_extra
    assert 'tts-edge = [' in pyproject
    assert '"edge-tts==7.2.8"' in pyproject
    assert '"edge-tts==7.2.8"' in all_extra
    assert "CosyVoice" not in all_extra

    assert 'REPO_ID = "hexgrad/Kokoro-82M-v1.1-zh"' in server
    assert 'DEFAULT_VOICES = list(provider_spec("kokoro").voices)' in server
    for voice in ("zf_001", "zf_002", "zf_003", "zf_004"):
        assert voice in prefetch
    assert "try_to_load_from_cache" in server
    assert "setup-tts-models.sh" in server
    assert '"http://127.0.0.1:9012"' in server
    assert '"http://127.0.0.1:9014"' in server
    assert '"http://127.0.0.1:9015"' in server
    assert '"Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"' in server
    assert '"gsv": GsvSidecarProvider' in server
    assert "class Qwen3SidecarProvider" not in server
    assert '"edge": EdgeTtsProvider' in server
    assert 'port = int(os.getenv("CHARACTER_TTS_LAB_PORT", "9002"))' in server

    assert "bash scripts/sync-all.sh" in setup_media
    assert "prefetch_embedding_model.py" in setup_media
    assert "prefetch_tts_models.py" in setup_media
    assert "exec bash scripts/setup-media-models.sh" in setup_tts
    assert '"kokoro-v1_1-zh.pth"' in prefetch
    assert '"config.json"' in prefetch

    assert "AutoModel" in sidecar
    assert "inference_sft" in sidecar
    assert "list_available_spks" in sidecar
    assert 'PORT = int(os.getenv("COSYVOICE_SIDECAR_PORT", "9012"))' in sidecar


def test_qwen3_is_not_registered_in_default_tts_lab_inventory():
    runtime = TtsLabRuntime()
    try:
        assert "qwen3" not in runtime.providers
        assert {"sherpa", "kokoro", "edge", "gsv"}.issubset(runtime.providers)
    finally:
        runtime.close()


def test_tts_lab_web_ui_exposes_provider_voice_and_ab_controls():
    html = Path("src/character_memory/web/tts_lab.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")

    assert 'id="ttsLabProvider"' in html
    assert 'id="ttsLabVoice"' in html
    assert 'id="generateTtsLab"' in html
    assert 'id="compareReady"' in html
    assert 'id="comparisonGrid"' in html
    assert 'id="voiceDesignRaw"' in html
    assert 'id="voiceDesignInstruct"' in html
    assert 'id="generateVoiceDesign"' in html
    assert 'id="polishVoiceDesign"' in html
    assert 'fetch("/v1/providers")' in script
    assert 'fetch("/v1/tts"' in script
    assert 'fetch("/v1/voice-design/status")' in script
    assert 'fetch("/v1/voice-design/polish"' in script
    assert 'fetch("/v1/voice-design/generate"' in script
    assert "decodeURIComponent" in script


# --- Voice Design -> character freeze contract -------------------------------
#
# VoiceDesign is not reproducible: the same text+instruct yields different audio
# every time. So the freeze must persist the exact bytes the user auditioned,
# addressed by the opaque artifact token returned from generate. These tests pin
# the wiring that keeps the stored transcript honest.

GUARD_MESSAGE = "测试文本或 Instruct 已修改，请重新生成后再固化。"


def _freeze_request_block(script: str) -> str:
    """Return the fetch options of the freeze call, up to its error handling."""
    after_url = script.split('"/v1/voice-design/freeze"', 1)[1]
    return after_url.split("if (!response.ok)", 1)[0]


def test_voice_design_output_column_exposes_character_picker_and_freeze_controls():
    html = Path("src/character_memory/web/tts_lab.html").read_text(encoding="utf-8")

    assert 'id="voiceDesignCharacter"' in html
    assert 'id="freezeVoiceDesign" disabled' in html
    assert 'id="voiceDesignFreezeStatus"' in html
    assert "固化到该角色" in html
    # The panel keeps its experimental marker; freezing must not promote it to
    # a realtime provider.
    css = Path("src/character_memory/web/tts_lab.css").read_text(encoding="utf-8")
    assert ".voice-design-card" in css
    assert "dashed" in css


def test_voice_design_freeze_reuses_the_existing_character_endpoint():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")

    # Characters come from the same route app.js uses, not a new one.
    assert 'fetch("/v1/characters")' in script
    assert '$("voiceDesignCharacter")' in script


def test_voice_design_generate_stores_the_artifact_token_from_the_response_header():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")

    assert "X-Voice-Design-Artifact" in script
    assert "voiceDesignArtifact" in script


def test_voice_design_freeze_posts_only_character_and_artifact_ids():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")
    block = _freeze_request_block(script)

    assert '"/v1/voice-design/freeze"' in script
    assert "character_id" in block
    assert "artifact_id" in block
    # The transcript is already recorded server-side with the artifact; resending
    # caller-supplied text would let the stored transcript drift from the audio.
    assert "text" not in block
    assert "instruct" not in block


def test_voice_design_freeze_status_surfaces_result_and_activation_state():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")

    assert '$("voiceDesignFreezeStatus")' in script
    assert "ref_audio" in script
    assert "voice_id" in script
    assert "activated" in script
    assert "reason" in script


def test_voice_design_freeze_result_survives_the_gate_re_render():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")
    body = script.split("async function freezeVoiceDesign(", 1)[1].split("\n  $(", 1)[0]

    # Re-arming the gate after a freeze must not clobber the outcome the user
    # is reading, so the displayed result is tracked explicitly.
    assert "voiceDesignFreezeResult" in script
    assert "voiceDesignFreezeResult = true" in body


def test_voice_design_freeze_button_is_gated_on_matching_snapshot_and_input_events():
    script = Path("src/character_memory/web/tts_lab.js").read_text(encoding="utf-8")

    assert GUARD_MESSAGE in script
    assert "voiceDesignSnapshot" in script
    # The guard re-evaluates on every edit, not only at generate time.
    for element in ("voiceDesignInstruct", "voiceDesignText"):
        assert f'$("{element}").addEventListener("input"' in script
    for element in ("voiceDesignLanguage", "voiceDesignCharacter"):
        assert f'$("{element}").addEventListener("change"' in script


def test_tts_lab_script_is_valid_javascript_when_node_is_available():
    node = shutil.which("node")
    if not node:
        return
    path = Path("src/character_memory/web/tts_lab.js")
    checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
