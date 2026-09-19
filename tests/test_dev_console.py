from pathlib import Path

from fastapi.testclient import TestClient

from character_memory.config import Settings
from character_memory.dev_server import create_dev_app


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, content=b"", headers=None, text=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text if text is not None else ("" if content else "{}")

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    @property
    def is_error(self):
        return self.status_code >= 400

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/v1/metrics/recent"):
            return FakeResponse({"metrics": [{"kind": "tts", "inference_ms": 20.0, "total_ms": 21.0}]})
        if ":8000/health" in url:
            return FakeResponse({"ok": True, "web": "ready", "runtime_loaded": False})
        if ":8001/health" in url:
            return FakeResponse(
                {
                    "ok": True,
                    "asr": {"ready": True, "loaded": False},
                    "tts": {"ready": True, "loaded": False},
                }
            )
        return FakeResponse({}, status_code=404, text="not found")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/v1/tts"):
            return FakeResponse(
                content=b"RIFFfake-wave",
                headers={
                    "x-media-provider": "fake-tts",
                    "x-media-device": "cpu",
                    "x-media-inference-ms": "12.3",
                    "x-media-audio-ms": "500.0",
                    "x-media-sample-rate": "16000",
                },
            )
        if url.endswith("/v1/asr"):
            return FakeResponse(
                {
                    "text": "测试成功",
                    "provider": "fake-asr",
                    "device": "cpu",
                    "inference_ms": 8.2,
                    "audio_ms": 500.0,
                }
            )
        return FakeResponse({}, status_code=404, text="not found")


class FailingTtsHttpClient(FakeHttpClient):
    def post(self, url, **kwargs):
        if url.endswith("/v1/tts"):
            return FakeResponse(
                {"detail": "VITS model failed to initialize"},
                status_code=503,
                text='{"detail":"VITS model failed to initialize"}',
            )
        return super().post(url, **kwargs)


class FakeModel:
    model = "fake-model"

    def _request(self, messages, *, conversation_id=None, **kwargs):
        assert conversation_id == "dev-console"
        assert messages[-1]["content"] == "ping"
        return "pong"

    def close(self):
        pass


def settings():
    return Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        chat_model="fake-model",
        embedding_provider="deterministic",
    )


def test_dev_console_assets_cover_runtime_test_surfaces():
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/dev.js").read_text(encoding="utf-8")
    assert "Dev Console" in html
    assert 'href="http://127.0.0.1:9002/tts"' in html
    assert "TTS Workbench :9002" in html
    assert ">LLM<" in html
    assert ">TTS<" in html
    assert ">ASR<" in html
    assert "Media Live Smoke" in html
    assert "Resource Monitor" in html
    assert 'value="60" selected' in html
    assert "Media Metrics" in html
    for endpoint in (
        "/v1/dev/status",
        "/v1/dev/resources",
        "/v1/dev/llm",
        "/v1/dev/tts",
        "/v1/dev/asr",
        "/v1/dev/media-smoke",
        "/v1/dev/metrics",
    ):
        assert endpoint in script


def test_dev_status_probes_character_and_media_without_loading_person_runtime():
    app = create_dev_app(settings=settings(), http_client=FakeHttpClient(), model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        response = client.get("/v1/dev/status")
    assert response.status_code == 200
    data = response.json()
    assert data["character"]["ok"] is True
    assert data["media"]["ok"] is True
    assert data["dev"]["model"] == "fake-model"
    assert "api_key" not in response.text
    assert "test-key" not in response.text


def test_dev_resources_are_best_effort_and_dependency_free():
    app = create_dev_app(settings=settings(), http_client=FakeHttpClient(), model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        response = client.get("/v1/dev/resources")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "system_memory" in data
    assert "gpu" in data
    assert "sampled_at" in data
    names = [item["name"] for item in data["processes"]]
    assert names == ["Character Runtime", "Media Runtime", "Dev Console"]


def test_dev_llm_uses_configured_provider_path():
    app = create_dev_app(settings=settings(), http_client=FakeHttpClient(), model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        response = client.post("/v1/dev/llm", json={"prompt": "ping", "system_prompt": ""})
    assert response.status_code == 200
    assert response.json()["reply"] == "pong"
    assert response.json()["model"] == "fake-model"
    assert response.json()["total_ms"] >= 0


def test_dev_tts_and_asr_proxy_media_contracts():
    fake_http = FakeHttpClient()
    app = create_dev_app(settings=settings(), http_client=fake_http, model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        tts = client.post("/v1/dev/tts", json={"text": "你好", "speaker_id": 0, "speed": 1.0})
        asr = client.post("/v1/dev/asr", content=b"RIFFfake-wave", headers={"Content-Type": "audio/wav"})
        metrics = client.get("/v1/dev/metrics?limit=10")
    assert tts.status_code == 200
    assert tts.content == b"RIFFfake-wave"
    assert tts.headers["x-media-provider"] == "fake-tts"
    assert "x-dev-total-ms" in tts.headers
    assert asr.status_code == 200
    assert asr.json()["text"] == "测试成功"
    assert asr.json()["http_total_ms"] >= 0
    assert metrics.json()["metrics"][0]["kind"] == "tts"


def test_dev_media_smoke_runs_real_contract_tts_then_asr():
    fake_http = FakeHttpClient()
    app = create_dev_app(settings=settings(), http_client=fake_http, model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        response = client.post(
            "/v1/dev/media-smoke",
            json={"text": "你好", "speaker_id": 0, "speed": 1.0},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["kind"] == "media-smoke"
    assert data["input_text"] == "你好"
    assert data["transcript"] == "测试成功"
    assert data["tts"]["provider"] == "fake-tts"
    assert data["asr"]["provider"] == "fake-asr"
    assert [call[0] for call in fake_http.calls if call[0] == "POST"] == ["POST", "POST"]


def test_dev_media_error_preserves_upstream_operation_status_and_detail():
    app = create_dev_app(settings=settings(), http_client=FailingTtsHttpClient(), model_factory=lambda _: FakeModel())
    with TestClient(app) as client:
        response = client.post("/v1/dev/tts", json={"text": "你好", "speaker_id": 0, "speed": 1.0})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["service"] == "media-runtime"
    assert detail["operation"] == "tts"
    assert detail["status_code"] == 503
    assert detail["detail"] == "VITS model failed to initialize"
