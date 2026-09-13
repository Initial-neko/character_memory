from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from character_memory.tts_lab import LabSynthesisResult, TtsLabRuntime, create_tts_lab_app


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


def test_tts_lab_provider_status_and_synthesis_contract():
    runtime = TtsLabRuntime({"fake": FakeTtsProvider()})
    app = create_tts_lab_app(runtime)
    with TestClient(app) as client:
        providers = client.get("/v1/providers")
        assert providers.status_code == 200
        assert providers.json()["providers"][0]["id"] == "fake"

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


def test_tts_lab_unknown_provider_is_a_client_error():
    app = create_tts_lab_app(TtsLabRuntime({"fake": FakeTtsProvider()}))
    with TestClient(app) as client:
        response = client.post(
            "/v1/tts",
            json={"provider": "missing", "text": "你好", "voice": "中文女", "speed": 1.0},
        )
        assert response.status_code == 400
        assert "Unknown TTS provider" in response.text


def test_tts_lab_static_provider_inventory_and_dependency_isolation():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    server = Path("src/character_memory/tts_lab.py").read_text(encoding="utf-8")
    sidecar = Path("scripts/cosyvoice_sidecar.py").read_text(encoding="utf-8")
    setup = Path("scripts/setup-tts-models.sh").read_text(encoding="utf-8")
    prefetch = Path("scripts/prefetch_tts_models.py").read_text(encoding="utf-8")

    assert 'tts-kokoro = [' in pyproject
    assert '"kokoro>=0.9.4,<1"' in pyproject
    assert '"misaki[zh]>=0.9.4,<1"' in pyproject
    assert '"huggingface-hub>=0.30,<2"' in pyproject
    assert 'character-tts-lab = "character_memory.tts_lab:main"' in pyproject
    all_extra = pyproject.split("all = [", 1)[1].split("\n]\n", 1)[0]
    assert '"kokoro>=0.9.4,<1"' in all_extra
    assert '"misaki[zh]>=0.9.4,<1"' in all_extra
    assert "CosyVoice" not in all_extra

    assert 'REPO_ID = "hexgrad/Kokoro-82M-v1.1-zh"' in server
    for voice in ("zf_001", "zf_002", "zf_003", "zf_004"):
        assert voice in server
        assert voice in prefetch
    for stale_voice in ("zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi"):
        assert stale_voice not in server
    assert "try_to_load_from_cache" in server
    assert "setup-tts-models.sh" in server
    assert '"http://127.0.0.1:9012"' in server
    assert 'port = int(os.getenv("CHARACTER_TTS_LAB_PORT", "9002"))' in server

    assert "bash scripts/sync-all.sh" in setup
    assert "prefetch_tts_models.py" in setup
    assert '"kokoro-v1_1-zh.pth"' in prefetch
    assert '"config.json"' in prefetch

    assert "AutoModel" in sidecar
    assert "inference_sft" in sidecar
    assert "list_available_spks" in sidecar
    assert 'PORT = int(os.getenv("COSYVOICE_SIDECAR_PORT", "9012"))' in sidecar


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
