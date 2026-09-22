import re
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

    def request(self, method, url, **kwargs):
        method = str(method).upper()
        if method == "GET":
            return self.get(url, **kwargs)
        if method == "POST":
            return self.post(url, **kwargs)
        return FakeResponse({}, status_code=405, text="method not allowed")

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


def test_every_element_id_a_web_script_reads_is_still_emitted_by_a_page():
    """``$("id")`` is a cross-file contract, and one broke with no error at all.

    The Dev Console's Media Live Smoke read its sample text, speaker and speed
    out of ``ttsText`` / ``speakerId`` / ``ttsSpeed``. A later pass deleted the
    legacy TTS card that was the only markup emitting those ids, so the button
    stayed visible and clickable while ``runMediaSmoke`` threw
    ``Cannot read properties of null`` on every press. Nothing but a live click
    could have caught it, because the script and the markup never mention each
    other. Checking every id a web script looks up against the ids the web
    markup emits turns the next such deletion into a failing test.
    """

    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "character_memory" / "web"
    emitted: set[str] = set()
    for page in sorted(web.glob("*.html")):
        emitted.update(re.findall(r'id="([^"]+)"', page.read_text(encoding="utf-8")))
    assert emitted, "the pages emit ids; the extraction above has gone stale"

    missing = {}
    for script_path in sorted(web.glob("*.js")):
        text = script_path.read_text(encoding="utf-8")
        for element_id in sorted(set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', text))):
            if element_id not in emitted:
                missing.setdefault(script_path.name, []).append(element_id)
    assert missing == {}, f"no page emits these ids any more: {missing}"


def test_dev_console_doc_keeps_up_with_the_runtime_only_scope():
    """The durable doc described a console that no longer exists.

    The Space card stopped persisting ``config.yaml`` and the legacy TTS card
    the doc described was deleted from the page, but ``docs/current`` is where
    AGENTS.md puts durable behavior and neither change reached it: the doc still
    told a reader their Dev tuning was persisted, and still described a TTS
    surface no page emits. The Random Encounter card and the archive/voice
    lifecycle landed with the same PR and are pinned here too.
    """

    doc = Path("docs/current/DEV_CONSOLE.md").read_text(encoding="utf-8")

    for stale in ("直接调整并持久化", "配置会写回 `config.yaml`", "### TTS\n"):
        assert stale not in doc, stale
    assert "不写 `config.yaml`" in doc, "the runtime-only scope has to be stated"

    script = Path("src/character_memory/web/dev.js").read_text(encoding="utf-8")
    for endpoint in (
        "/v1/dev/encounters/status",
        "/v1/dev/encounters/opportunity",
        "/v1/dev/encounters/due",
    ):
        assert endpoint in script
        assert endpoint in doc, f"{endpoint} is documented nowhere"

    # Archiving is a voice-lifecycle change, not a deletion, and the doc says
    # which of the two the running GSV sidecar actually sees.
    assert "409" in doc
    assert "reloaded" in doc


def test_dev_console_assets_cover_runtime_test_surfaces():
    html = Path("src/character_memory/web/dev.html").read_text(encoding="utf-8")
    script = Path("src/character_memory/web/dev.js").read_text(encoding="utf-8")
    assert "Dev Console" in html
    assert 'href="http://127.0.0.1:9002/tts"' in html
    assert "TTS Workbench :9002" in html
    assert ">LLM<" in html
    assert ">TTS<" not in html
    assert ">ASR<" in html
    assert "Media Live Smoke" in html
    assert "Resource Monitor" in html
    assert 'value="60" selected' in html
    assert "Media Metrics" in html
    for endpoint in (
        "/v1/dev/status",
        "/v1/dev/resources",
        "/v1/dev/llm",
        "/v1/dev/encounters/status",
        "/v1/dev/encounters/opportunity",
        "/v1/dev/encounters/due",
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


class RuntimeConfigHttpClient(FakeHttpClient):
    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/v1/space/dev/config"):
            return FakeResponse({"enabled": kwargs.get("json", {}).get("enabled"), "interval_minutes": 10})
        if url.endswith("/v1/group-autonomy/config"):
            return FakeResponse({"enabled": kwargs.get("json", {}).get("enabled"), "interval_minutes": 30})
        if url.endswith("/v1/encounters/dev/due"):
            return FakeResponse({"state": {"next_opportunity_at": "now"}})
        if url.endswith("/v1/encounters/dev/opportunity"):
            return FakeResponse({"source_type": kwargs.get("json", {}).get("source_type"), "candidate": {"id": 1}})
        return super().post(url, **kwargs)

    def get(self, url, **kwargs):
        if url.endswith("/v1/encounters/status"):
            self.calls.append(("GET", url, kwargs))
            return FakeResponse({"enabled": True, "pending_count": 1})
        return super().get(url, **kwargs)


def test_dev_autonomy_tuning_is_runtime_only_and_does_not_modify_config(tmp_path):
    config = tmp_path / "config.yaml"
    original = (
        "space_opportunity_interval_minutes: 1440\n"
        "group_autonomy_interval_minutes: 360\n"
    )
    config.write_text(original, encoding="utf-8")
    fake_http = RuntimeConfigHttpClient()
    app = create_dev_app(
        str(config),
        settings=settings(),
        http_client=fake_http,
        model_factory=lambda _: FakeModel(),
    )

    with TestClient(app) as client:
        space = client.post(
            "/v1/dev/space/config",
            json={
                "enabled": True,
                "interval_minutes": 10,
                "max_posts_per_day": 0,
                "media_enabled": True,
                "media_max_items": 3,
                "image_search_enabled": True,
                "image_generation_enabled": True,
                "world_observation_enabled": True,
                "world_max_pages": 2,
                "world_max_chars_per_page": 6000,
                "audience_size": 5,
                "poll_seconds": 60,
                "rearm": True,
            },
        )
        group = client.post(
            "/v1/dev/group-autonomy/config",
            json={
                "enabled": True,
                "interval_minutes": 30,
                "max_messages": 3,
                "user_quiet_minutes": 30,
                "poll_seconds": 60,
                "rearm": True,
            },
        )

    assert space.status_code == 200
    assert group.status_code == 200
    assert space.json()["scope"] == "runtime-only"
    assert group.json()["scope"] == "runtime-only"
    assert space.json()["persisted"] is False
    assert group.json()["persisted"] is False
    assert config.read_text(encoding="utf-8") == original


def test_dev_encounter_endpoints_are_diagnostic_proxies():
    fake_http = RuntimeConfigHttpClient()
    app = create_dev_app(settings=settings(), http_client=fake_http, model_factory=lambda _: FakeModel())

    with TestClient(app) as client:
        status = client.get("/v1/dev/encounters/status")
        generated = client.post("/v1/dev/encounters/opportunity?source_type=WEB")
        due = client.post("/v1/dev/encounters/due")

    assert status.status_code == 200
    assert status.json()["pending_count"] == 1
    assert generated.status_code == 200
    assert generated.json()["source_type"] == "WEB"
    assert due.status_code == 200
