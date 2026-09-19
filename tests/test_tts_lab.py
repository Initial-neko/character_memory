from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from character_memory.tts_lab import EdgeTtsProvider, GsvSidecarProvider, LabSynthesisResult, Qwen3SidecarProvider, SherpaMediaProvider, TtsLabRuntime, create_tts_lab_app


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


class _FakeQwenClient:
    def __init__(self):
        self.posts = []

    def get(self, *args, **kwargs):
        return _FakeQwenResponse(
            {
                "ready": True,
                "loaded": True,
                "voices": ["Vivian", "Serena"],
                "default_voice": "Vivian",
                "model": "fake-qwen3",
                "device": "cuda:0",
                "reason": None,
            }
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeQwenResponse(
            content=b"RIFFqwen",
            headers={
                "x-tts-voice": "Vivian",
                "x-tts-model": "fake-qwen3",
                "x-tts-device": "cuda:0",
                "x-tts-inference-ms": "88.8",
                "x-tts-audio-ms": "800",
                "x-tts-sample-rate": "24000",
            },
        )

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



def test_qwen3_sidecar_provider_status_and_synthesis():
    client = _FakeQwenClient()
    provider = Qwen3SidecarProvider(client=client)

    status = provider.status()
    assert status["ready"] is True
    assert status["loaded"] is True
    assert status["default_voice"] == "Vivian"
    assert status["device"] == "cuda:0"

    result = provider.synthesize("你好", voice="Vivian", speed=1.0)
    assert result.audio == b"RIFFqwen"
    assert result.provider == "qwen3"
    assert result.voice == "Vivian"
    assert result.device == "cuda:0"
    assert result.inference_ms == 88.8
    assert client.posts == [
        (
            "http://127.0.0.1:9013/v1/tts",
            {
                "json": {
                    "text": "你好",
                    "voice": "Vivian",
                    "language": "Chinese",
                    "speed": 1.0,
                },
                "timeout": 180.0,
            },
        )
    ]


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
    assert 'DEFAULT_VOICES = ["zf_001", "zf_002", "zf_003", "zf_004"]' in server
    for voice in ("zf_001", "zf_002", "zf_003", "zf_004"):
        assert voice in prefetch
    assert "try_to_load_from_cache" in server
    assert "setup-tts-models.sh" in server
    assert '"http://127.0.0.1:9012"' in server
    assert '"http://127.0.0.1:9014"' in server
    assert '"gsv": GsvSidecarProvider' in server
    assert '"qwen3": Qwen3SidecarProvider' not in server
    assert '"edge": EdgeTtsProvider' in server
    assert 'port = int(os.getenv("CHARACTER_TTS_LAB_PORT", "9002"))' in server

    assert "bash scripts/sync-all.sh" in setup_media
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
    assert 'fetch("/v1/providers")' in script
    assert 'fetch("/v1/tts"' in script
    assert "decodeURIComponent" in script
