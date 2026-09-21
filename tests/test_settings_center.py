from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from character_memory.config import load_settings
from character_memory.envfile import parse_env_file
from character_memory.settings_server import create_settings_app
from character_memory.settings_store import SettingsStore


def _clear_secret_env(monkeypatch):
    for name in (
        "OPENCODE_GO_API_KEY",
        "EMBEDDING_API_KEY",
        "SEARCHAPI_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "AGNES_API_KEY",
        "MSIMG_API_KEY",
        "MODELSCOPE_API_TOKEN",
        "HF_TOKEN",
        "GSV_TTS_GPT_MODEL",
        "GSV_TTS_SOVITS_MODEL",
        "GSV_TTS_VOICE",
    ):
        monkeypatch.delenv(name, raising=False)


class _RuntimeResponse:
    def __init__(self, payload=None, status_code=200, *, content=b"", headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = ""
        self.content = content
        self.headers = headers or {}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _SettingsRuntimeClient:
    def __init__(self, providers=None):
        items = providers or [
            {
                "id": "kokoro",
                "label": "Kokoro 82M v1.1 zh",
                "ready": True,
                "loaded": False,
                "voices": ["zf_001", "zf_002", "zf_003", "zf_004"],
                "default_voice": "zf_001",
                "supports_speed": True,
                "device": "cpu",
                "model": "kokoro",
                "reason": None,
            },
            {
                "id": "sherpa",
                "label": "Sherpa VITS",
                "ready": True,
                "loaded": False,
                "voices": ["0", "2", "5"],
                "default_voice": "0",
                "supports_speed": True,
                "device": "cpu",
                "model": "sherpa",
                "reason": None,
            },
            {
                "id": "edge",
                "label": "Microsoft Edge TTS (online)",
                "ready": False,
                "loaded": False,
                "voices": ["zh-CN-XiaoxiaoNeural"],
                "default_voice": "zh-CN-XiaoxiaoNeural",
                "supports_speed": True,
                "device": "cloud",
                "model": "edge",
                "reason": "edge_tts unavailable",
            },
            {
                "id": "gsv",
                "label": "GSV-TTS-Lite",
                "ready": True,
                "loaded": False,
                "voices": ["murasame"],
                "default_voice": "murasame",
                "supports_speed": True,
                "device": "cuda",
                "model": "gsv",
                "reason": None,
            },
        ]
        self.providers = {item["id"]: item for item in items}
        self.post_calls = []

    def get(self, url, **kwargs):
        marker = "/v1/providers/"
        if marker in url:
            provider_id = url.rsplit(marker, 1)[1]
            item = self.providers.get(provider_id)
            if item is None:
                return _RuntimeResponse({"detail": "missing"}, status_code=404)
            return _RuntimeResponse({"provider": item})
        return _RuntimeResponse({"ok": True})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _RuntimeResponse(
            content=b"RIFFpreview",
            headers={
                "content-type": "audio/wav",
                "x-tts-voice": "murasame",
                "x-tts-device": "cuda:0",
            },
        )


def test_legacy_secrets_move_to_env_without_plaintext_backup(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    config.write_text(
        "# keep this comment\n"
        'api_key: "legacy-llm-secret"\n'
        'search_provider: "searchapi"\n'
        'search_api_key: "legacy-search-secret"\n'
        'agnes_api_key: "legacy-agnes-secret"\n'
        'chat_model: "deepseek-flash"\n'
        'custom_extension_key: "keep-me"\n',
        encoding="utf-8",
    )

    store = SettingsStore(str(config), str(env))
    result = store.migrate_legacy_secrets()

    assert result["changed"] is True
    values = parse_env_file(env)
    assert values["OPENCODE_GO_API_KEY"] == "legacy-llm-secret"
    assert values["SEARCHAPI_API_KEY"] == "legacy-search-secret"
    assert values["AGNES_API_KEY"] == "legacy-agnes-secret"

    migrated_config = config.read_text(encoding="utf-8")
    assert "# keep this comment" in migrated_config
    assert "custom_extension_key" in migrated_config
    assert "legacy-llm-secret" not in migrated_config
    assert "api_key:" not in migrated_config
    backup = Path(result["backup"])
    assert backup.is_file()
    backup_text = backup.read_text(encoding="utf-8")
    assert "legacy-llm-secret" not in backup_text
    assert "legacy-search-secret" not in backup_text
    assert "legacy-agnes-secret" not in backup_text


def test_settings_save_preserves_comments_unknown_keys_and_creates_backup(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        "# user comment\n"
        'chat_model: "deepseek-flash"\n'
        'tts_provider: "sherpa"\n'
        'tts_voice: "0"\n'
        'custom_extension_key: "keep-me"\n',
        encoding="utf-8",
    )
    store = SettingsStore(str(config), str(tmp_path / ".env"))

    result = store.save_values({"tts_provider": "gsv", "tts_voice": "murasame", "tts_device": "cuda"})

    assert result["changed"] is True
    assert Path(result["backup"]).is_file()
    text = config.read_text(encoding="utf-8")
    assert "# user comment" in text
    assert 'custom_extension_key: "keep-me"' in text
    assert 'tts_provider: "gsv"' in text
    assert 'tts_voice: "murasame"' in text
    assert 'tts_device: "cuda"' in text
    settings = load_settings(str(config))
    assert settings.tts_provider == "gsv"
    assert settings.tts_voice == "murasame"
    assert settings.tts_device == "cuda"


def test_gsv_runtime_fields_persist_to_dotenv_without_overwriting_yaml_or_existing_env(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    config.write_text(
        '# keep yaml\n'
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n'
        'custom_extension_key: "keep-me"\n',
        encoding="utf-8",
    )
    env.write_text('EXISTING_VALUE="keep-me"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(env))

    result = store.save_values(
        {
            "GSV_TTS_GPT_MODEL": "C:/models/voice.ckpt",
            "GSV_TTS_SOVITS_MODEL": "C:/models/voice.pth",
            "GSV_TTS_VOICE": "murasame",
        }
    )

    assert result["changed"] is True
    assert result["restart_required"] == []
    yaml_text = config.read_text(encoding="utf-8")
    assert '# keep yaml' in yaml_text
    assert 'custom_extension_key: "keep-me"' in yaml_text
    assert "GSV_TTS_GPT_MODEL" not in yaml_text

    env_values = parse_env_file(env)
    assert env_values["EXISTING_VALUE"] == "keep-me"
    assert env_values["GSV_TTS_GPT_MODEL"] == "C:/models/voice.ckpt"
    assert env_values["GSV_TTS_SOVITS_MODEL"] == "C:/models/voice.pth"
    assert env_values["GSV_TTS_VOICE"] == "murasame"

    snapshot = store.snapshot()
    assert snapshot["values"]["GSV_TTS_GPT_MODEL"] == "C:/models/voice.ckpt"
    assert snapshot["values"]["GSV_TTS_VOICE"] == "murasame"


def test_qwen3_is_not_a_formal_tts_provider(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "kokoro"\ntts_voice: "zf_001"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(tmp_path / ".env"))

    with pytest.raises(ValueError):
        store.save_values({"tts_provider": "qwen3"})


def test_gsv_is_an_accepted_tts_provider(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "kokoro"\ntts_voice: "zf_001"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(tmp_path / ".env"))

    result = store.save_values({"tts_provider": "gsv"})

    assert result["changed"] is True
    assert load_settings(str(config)).tts_provider == "gsv"
    schema = store.snapshot()["schema"]
    voice = next(section for section in schema if section["id"] == "voice")
    provider = next(field for field in voice["fields"] if field["name"] == "tts_provider")
    assert any(option["value"] == "gsv" for option in provider["options"])


def test_system_environment_overrides_dotenv(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('chat_model: "deepseek-flash"\n', encoding="utf-8")
    (tmp_path / ".env").write_text('OPENCODE_GO_API_KEY="dotenv-value"\n', encoding="utf-8")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "system-value")

    settings = load_settings(str(config))

    assert settings.api_key == "system-value"


def test_settings_api_never_returns_secret_value(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'chat_model: "deepseek-flash"\n'
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n',
        encoding="utf-8",
    )
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(str(config), store=store, runtime_http_client=_SettingsRuntimeClient())

    with TestClient(app) as client:
        initial = client.get("/v1/settings")
        assert initial.status_code == 200
        assert initial.json()["values"]["tts_provider"] == "kokoro"

        secret_value = "super-private-test-key"
        saved = client.put(
            "/v1/settings/secrets/OPENCODE_GO_API_KEY",
            json={"value": secret_value},
        )
        assert saved.status_code == 200
        assert secret_value not in saved.text

        snapshot = client.get("/v1/settings")
        assert snapshot.status_code == 200
        assert secret_value not in snapshot.text
        secret = next(
            item for item in snapshot.json()["secrets"]
            if item["name"] == "OPENCODE_GO_API_KEY"
        )
        assert secret["configured"] is True
        assert secret["value"] is None

        patched = client.patch(
            "/v1/settings",
            json={"values": {"tts_voice": "zf_004", "tts_speed": 1.1}},
        )
        assert patched.status_code == 200
        assert patched.json()["result"]["restart_required"] == []


def test_settings_tts_options_are_health_gated_and_provider_aware(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "kokoro"\ntts_voice: "zf_001"\ntts_device: "cpu"\n', encoding="utf-8")
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=_SettingsRuntimeClient(),
    )

    with TestClient(app) as client:
        snapshot = client.get("/v1/settings").json()
        voice_section = next(section for section in snapshot["schema"] if section["id"] == "voice")
        provider_field = next(field for field in voice_section["fields"] if field["name"] == "tts_provider")
        voice_field = next(field for field in voice_section["fields"] if field["name"] == "tts_voice")

        options = {item["value"]: item for item in provider_field["options"]}
        assert options["kokoro"]["disabled"] is False
        assert options["sherpa"]["disabled"] is False
        assert options["gsv"]["disabled"] is False
        assert options["edge"]["disabled"] is True
        assert "qwen3" not in options
        assert [item["value"] for item in voice_field["options"]] == ["zf_001", "zf_002", "zf_003", "zf_004"]


def test_one_provider_health_failure_does_not_hide_other_healthy_providers(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "kokoro"\ntts_voice: "zf_001"\n', encoding="utf-8")

    class PartialFailureClient(_SettingsRuntimeClient):
        def get(self, url, **kwargs):
            if url.endswith("/v1/providers/edge"):
                raise RuntimeError("edge probe timeout")
            return super().get(url, **kwargs)

    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=PartialFailureClient(),
    )

    with TestClient(app) as client:
        snapshot = client.get("/v1/settings").json()

    voice_section = next(section for section in snapshot["schema"] if section["id"] == "voice")
    provider_field = next(field for field in voice_section["fields"] if field["name"] == "tts_provider")
    options = {item["value"]: item for item in provider_field["options"]}
    assert options["kokoro"]["disabled"] is False
    assert options["sherpa"]["disabled"] is False
    assert options["gsv"]["disabled"] is False
    assert options["edge"]["disabled"] is True
    assert "edge probe timeout" in snapshot["tts"]["error"]


def test_kokoro_device_change_is_persisted_but_requires_tts_runtime_restart(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n'
        'tts_device: "cpu"\n',
        encoding="utf-8",
    )
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=_SettingsRuntimeClient(),
    )

    with TestClient(app) as client:
        response = client.patch(
            "/v1/settings",
            json={"values": {"tts_device": "cuda"}},
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["persisted"] is True
    assert result["restart_required"] == ["tts_device"]
    assert result["runtime_apply"]["applied"] is True
    assert result["runtime_apply"]["restart_required"] == ["tts_device"]
    assert load_settings(str(config)).tts_device == "cuda"


def test_persisted_gsv_selection_reports_runtime_apply_failure_separately(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n'
        'tts_device: "cpu"\n',
        encoding="utf-8",
    )

    class FailOnPreloadClient(_SettingsRuntimeClient):
        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            if url.endswith("/v1/configure"):
                if kwargs.get("json", {}).get("preload"):
                    raise RuntimeError("CUDA OOM during GSV preload")
                self.providers["gsv"]["ready"] = True
                self.providers["gsv"]["reason"] = None
                return _RuntimeResponse({"ready": True, "loaded": False})
            return super().post(url, **kwargs)

    runtime = FailOnPreloadClient()
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=runtime,
    )

    with TestClient(app) as client:
        response = client.patch(
            "/v1/settings",
            json={
                "values": {
                    "tts_provider": "gsv",
                    "tts_voice": "murasame",
                    "tts_device": "cuda",
                }
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["persisted"] is True
    assert body["result"]["runtime_apply"]["applied"] is False
    assert "CUDA OOM" in body["result"]["runtime_apply"]["error"]
    assert load_settings(str(config)).tts_provider == "gsv"


def test_provider_change_normalizes_stale_voice_and_device(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_002"\n'
        'tts_device: "cpu"\n',
        encoding="utf-8",
    )
    runtime = _SettingsRuntimeClient()
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=runtime,
    )

    with TestClient(app) as client:
        response = client.patch(
            "/v1/settings",
            json={
                "values": {
                    "tts_provider": "gsv",
                    "tts_voice": "zf_002",
                    "tts_device": "cpu",
                }
            },
        )

    assert response.status_code == 200
    values = response.json()["settings"]["values"]
    assert values["tts_provider"] == "gsv"
    assert values["tts_voice"] == "murasame"
    assert values["tts_device"] == "cuda"


def test_gsv_runtime_can_be_configured_and_selected_in_one_save_without_stack_restart(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'tts_provider: "kokoro"\n'
        'tts_voice: "zf_001"\n'
        'tts_device: "cpu"\n',
        encoding="utf-8",
    )

    class ConfigurableClient(_SettingsRuntimeClient):
        def __init__(self):
            super().__init__()
            self.providers["gsv"]["ready"] = False
            self.providers["gsv"]["reason"] = "Missing GSV configuration"

        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            if url.endswith("/v1/configure"):
                payload = kwargs["json"]
                assert payload["gpt_model"] == "C:/models/voice.ckpt"
                assert payload["sovits_model"] == "C:/models/voice.pth"
                # The reference is owned by a template now. The key has to be
                # *absent*: ``configure`` reads "" as "overwrite" and would blank
                # the warm engine's reference, which surfaces as "GSV got worse".
                assert "ref_audio" not in payload
                assert "ref_text" not in payload
                assert payload["device"] == "cuda"
                self.providers["gsv"]["ready"] = True
                self.providers["gsv"]["reason"] = None
                return _RuntimeResponse({"ready": True, "loaded": True})
            if url.endswith("/v1/load"):
                return _RuntimeResponse({"ready": True, "loaded": True})
            return super().post(url, **kwargs)

    runtime = ConfigurableClient()
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(str(config), store=store, runtime_http_client=runtime)

    with TestClient(app) as client:
        response = client.patch(
            "/v1/settings",
            json={
                "values": {
                    "GSV_TTS_GPT_MODEL": "C:/models/voice.ckpt",
                    "GSV_TTS_SOVITS_MODEL": "C:/models/voice.pth",
                    "GSV_TTS_VOICE": "murasame",
                    "tts_provider": "gsv",
                    "tts_voice": "zf_001",
                    "tts_device": "cpu",
                }
            },
        )

    assert response.status_code == 200
    values = response.json()["settings"]["values"]
    assert values["tts_provider"] == "gsv"
    assert values["tts_voice"] == "murasame"
    assert values["tts_device"] == "cuda"
    assert response.json()["result"]["restart_required"] == []

    env_values = parse_env_file(tmp_path / ".env")
    assert env_values["GSV_TTS_GPT_MODEL"] == "C:/models/voice.ckpt"
    assert env_values["GSV_TTS_VOICE"] == "murasame"


def test_settings_tts_preview_calls_selected_healthy_provider(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        'tts_provider: "gsv"\n'
        'tts_voice: "murasame"\n'
        'tts_device: "cuda"\n',
        encoding="utf-8",
    )
    runtime = _SettingsRuntimeClient()
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/tts-preview",
            json={
                "provider": "gsv",
                "voice": "murasame",
                "speed": 1.0,
                "text": "你好，这是试听。",
            },
        )

    assert response.status_code == 200
    assert response.content == b"RIFFpreview"
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-tts-provider"] == "gsv"
    assert response.headers["x-tts-voice"] == "murasame"
    assert response.headers["x-tts-device"] == "cuda:0"
    assert runtime.post_calls == [
        (
            "http://127.0.0.1:9002/v1/tts",
            {
                "json": {
                    "provider": "gsv",
                    "text": "你好，这是试听。",
                    "voice": "murasame",
                    "speed": 1.0,
                },
                "timeout": 180.0,
            },
        )
    ]


def test_settings_rejects_unhealthy_provider_and_autoselects_healthy_default_voice(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "kokoro"\ntts_voice: "zf_001"\ntts_device: "cpu"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(
        str(config),
        store=store,
        runtime_http_client=_SettingsRuntimeClient(),
    )

    with TestClient(app) as client:
        rejected = client.patch("/v1/settings", json={"values": {"tts_provider": "edge"}})
        assert rejected.status_code == 400
        assert "health check did not pass" in rejected.text

        accepted = client.patch("/v1/settings", json={"values": {"tts_provider": "gsv", "tts_device": "cuda"}})
        assert accepted.status_code == 200
        assert accepted.json()["settings"]["values"]["tts_provider"] == "gsv"
        assert accepted.json()["settings"]["values"]["tts_voice"] == "murasame"

    settings = load_settings(str(config))
    assert settings.tts_provider == "gsv"
    assert settings.tts_voice == "murasame"
    assert settings.tts_device == "cuda"


def test_settings_rejects_voice_not_reported_by_healthy_provider(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\ntts_voice: "murasame"\n', encoding="utf-8")
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=_SettingsRuntimeClient(),
    )

    with TestClient(app) as client:
        response = client.patch("/v1/settings", json={"values": {"tts_voice": "zf_001"}})

    assert response.status_code == 400
    assert "not available for healthy provider" in response.text


def test_settings_center_and_formal_tts_wiring_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    stack = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    media = Path("src/character_memory/media_server.py").read_text(encoding="utf-8")
    settings_store = Path("src/character_memory/settings_store.py").read_text(encoding="utf-8")
    registry = Path("src/character_memory/tts_registry.py").read_text(encoding="utf-8")
    settings_server = Path("src/character_memory/settings_server.py").read_text(encoding="utf-8")
    settings_js = Path("src/character_memory/web/settings.js").read_text(encoding="utf-8")
    chat = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    lab = Path("src/character_memory/web/tts_lab.html").read_text(encoding="utf-8")

    assert 'character-settings = "character_memory.settings_server:main"' in pyproject
    assert "127.0.0.1:8003/health" in stack
    assert 'choices=("dev", "chat", "settings", "tts")' in stack
    assert '"character_memory.settings_server"' in stack
    assert 'settings.tts_provider' in media
    assert 'FORMAL_TTS_PROVIDER_SET' in media
    assert '"qwen3"' not in registry
    assert 'id="edge"' in registry
    assert 'label="Microsoft Edge TTS (online)"' in registry
    assert 'id="gsv"' in registry
    assert 'label="GSV-TTS-Lite (local)"' in registry
    assert 'FORMAL_TTS_PROVIDERS' in settings_store
    assert 'FORMAL_TTS_PROVIDER_IDS' in settings_server
    assert '/v1/providers/{provider_id}' in settings_server
    assert "health check did not pass" in settings_server
    assert "tts-health-status" in settings_js
    assert "option.disabled" in settings_js
    assert 'device.value = detected' in settings_js
    assert '/v1/tts-preview' in settings_js
    assert 'Cloud (Provider managed)' in settings_js
    assert "runtime_apply" in settings_js
    assert "需重启对应 TTS Runtime" in settings_js
    assert "zh-CN-XiaoxiaoNeural" in registry
    assert 'http://127.0.0.1:8003/settings' in chat
    assert 'http://127.0.0.1:8003/settings' in lab


def _gsv_template_field(app, config_path, env_path, client):
    with TestClient(app) as test_client:
        snapshot = test_client.get("/v1/settings").json()
    section = next(item for item in snapshot["schema"] if item["id"] == "gsv-runtime")
    return next(field for field in section["fields"] if field["name"] == "GSV_TTS_VOICE")


def test_a_missing_default_template_does_not_lock_the_field_that_fixes_it(tmp_path: Path, monkeypatch):
    """The one value that repairs GSV must stay enterable while GSV is broken.

    Readiness is "the default template loads", so a GSV whose default template
    is missing reports ``ready: false`` -- and that is precisely the state this
    selector exists to repair. Disabling its options on readiness left the
    operator looking at the template they needed, greyed out, on a page whose
    save path was already written to apply a new template before health gating
    it: the only way out was to hand-edit .env.
    """

    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\ntts_voice: "momo"\n', encoding="utf-8")

    class BrokenDefaultClient(_SettingsRuntimeClient):
        def __init__(self):
            super().__init__()
            self.providers["gsv"]["ready"] = False
            self.providers["gsv"]["reason"] = (
                "Default template 'murasame' is not defined; create one in the TTS Lab "
                "voice design page or pick an existing template in Settings Center."
            )
            # What the sidecar reports: the registered templates plus the
            # configured default, which need not exist.
            self.providers["gsv"]["voices"] = ["character-c70f8f95", "momo", "rei", "murasame"]

    runtime = BrokenDefaultClient()
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(str(config), store=store, runtime_http_client=runtime)

    field = _gsv_template_field(app, config, tmp_path / ".env", runtime)
    options = {item["value"]: item for item in field["options"]}
    assert options["momo"]["disabled"] is False
    assert options["rei"]["disabled"] is False

    with TestClient(app) as client:
        response = client.patch("/v1/settings", json={"values": {"GSV_TTS_VOICE": "momo"}})

    assert response.status_code == 200
    assert parse_env_file(tmp_path / ".env")["GSV_TTS_VOICE"] == "momo"
    # Applied to the running sidecar, not merely persisted for the next start.
    configure = [call for call in runtime.post_calls if call[0].endswith("/v1/configure")]
    assert configure, "saving the default template must reach the running sidecar"
    assert {call[1]["json"]["voice"] for call in configure} == {"momo"}


def test_a_silent_sidecar_says_so_instead_of_offering_nothing(tmp_path: Path, monkeypatch):
    """An unreachable sidecar lists no templates; the dropdown has to say why.

    "Create one in the TTS Lab" is the wrong instruction when the reason the
    list is empty is that nothing answered -- the templates may be sitting right
    there, and the user would go looking for a problem that is not theirs.
    """

    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\ntts_voice: "momo"\n', encoding="utf-8")

    class SilentGsvClient(_SettingsRuntimeClient):
        def get(self, url, **kwargs):
            if url.endswith("/v1/providers/gsv"):
                raise RuntimeError("connection refused")
            return super().get(url, **kwargs)

    runtime = SilentGsvClient()
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(str(config), store=store, runtime_http_client=runtime)

    field = _gsv_template_field(app, config, tmp_path / ".env", runtime)
    assert [item["disabled"] for item in field["options"]] == [True]
    assert "未应答" in field["options"][0]["label"]


def test_a_working_provider_with_an_unusable_default_template_says_so(tmp_path: Path, monkeypatch):
    """The warning has to survive the hop from the sidecar to the card.

    A provider whose default template is missing is *working* -- every template
    it lists resolves -- so it must not be shown as unavailable. It also must
    not be shown as simply fine: only a character with no voice of its own
    reaches that default, and the operator needs to know that before one of
    them goes silent.
    """

    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('tts_provider: "gsv"\ntts_voice: "momo"\n', encoding="utf-8")

    class WarnedClient(_SettingsRuntimeClient):
        def __init__(self):
            super().__init__()
            self.providers["gsv"]["default_template_problem"] = (
                "Default template 'murasame' is not defined; pick an existing template."
            )

    runtime = WarnedClient()
    app = create_settings_app(
        str(config),
        store=SettingsStore(str(config), str(tmp_path / ".env")),
        runtime_http_client=runtime,
    )

    with TestClient(app) as client:
        snapshot = client.get("/v1/settings").json()

    gsv = next(item for item in snapshot["tts"]["providers"] if item["id"] == "gsv")
    assert gsv["ready"] is True, "a warning must not read as a refusal"
    assert "murasame" in gsv["default_template_problem"]

    settings_js = (Path(__file__).resolve().parents[1] / "src/character_memory/web/settings.js").read_text(encoding="utf-8")
    assert "item.default_template_problem" in settings_js


def test_space_autonomy_settings_are_editable_from_settings_center(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('chat_model: "deepseek-flash"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(tmp_path / ".env"))
    app = create_settings_app(
        str(config),
        store=store,
        runtime_http_client=_SettingsRuntimeClient(),
    )

    with TestClient(app) as client:
        snapshot = client.get("/v1/settings").json()
        section = next(item for item in snapshot["schema"] if item["id"] == "space-autonomy")
        assert [field["name"] for field in section["fields"]] == [
            "space_autonomy_enabled",
            "space_opportunity_interval_minutes",
            "space_audience_size",
            "space_scheduler_poll_seconds",
        ]

        response = client.patch(
            "/v1/settings",
            json={
                "values": {
                    "space_autonomy_enabled": True,
                    "space_opportunity_interval_minutes": 60,
                    "space_audience_size": 2,
                    "space_scheduler_poll_seconds": 20,
                }
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert set(result["restart_required"]) == {
        "space_autonomy_enabled",
        "space_opportunity_interval_minutes",
        "space_audience_size",
        "space_scheduler_poll_seconds",
    }
    settings = load_settings(str(config))
    assert settings.space_opportunity_interval_minutes == 60
    assert settings.space_audience_size == 2
    assert settings.space_scheduler_poll_seconds == 20


def test_space_autonomy_settings_reject_too_short_interval(tmp_path: Path, monkeypatch):
    _clear_secret_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text('chat_model: "deepseek-flash"\n', encoding="utf-8")
    store = SettingsStore(str(config), str(tmp_path / ".env"))

    with pytest.raises(ValueError):
        store.save_values({"space_opportunity_interval_minutes": 5})
