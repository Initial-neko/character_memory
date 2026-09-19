from pathlib import Path

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
    ):
        monkeypatch.delenv(name, raising=False)


class _RuntimeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

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
        self.providers = providers or [
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
            {
                "id": "qwen3",
                "label": "Qwen3-TTS 0.6B",
                "ready": False,
                "loaded": False,
                "voices": ["Vivian"],
                "default_voice": "Vivian",
                "supports_speed": False,
                "device": "cuda:0",
                "model": "qwen3",
                "reason": "sidecar unavailable",
            },
        ]

    def get(self, url, **kwargs):
        if url.endswith("/v1/providers"):
            return _RuntimeResponse({"providers": self.providers})
        return _RuntimeResponse({"ok": True})


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

    result = store.save_values({"tts_provider": "qwen3", "tts_voice": "Vivian", "tts_device": "cuda"})

    assert result["changed"] is True
    assert Path(result["backup"]).is_file()
    text = config.read_text(encoding="utf-8")
    assert "# user comment" in text
    assert 'custom_extension_key: "keep-me"' in text
    assert 'tts_provider: "qwen3"' in text
    assert 'tts_voice: "Vivian"' in text
    assert 'tts_device: "cuda"' in text
    settings = load_settings(str(config))
    assert settings.tts_provider == "qwen3"
    assert settings.tts_voice == "Vivian"
    assert settings.tts_device == "cuda"


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
        assert patched.json()["result"]["restart_required"] == ["tts_speed", "tts_voice"]


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
        assert options["gsv"]["disabled"] is False
        assert options["qwen3"]["disabled"] is True
        assert [item["value"] for item in voice_field["options"]] == ["zf_001", "zf_002", "zf_003", "zf_004"]


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
        rejected = client.patch("/v1/settings", json={"values": {"tts_provider": "qwen3"}})
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
    settings_server = Path("src/character_memory/settings_server.py").read_text(encoding="utf-8")
    settings_js = Path("src/character_memory/web/settings.js").read_text(encoding="utf-8")
    chat = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    lab = Path("src/character_memory/web/tts_lab.html").read_text(encoding="utf-8")

    assert 'character-settings = "character_memory.settings_server:main"' in pyproject
    assert "127.0.0.1:8003/health" in stack
    assert 'choices=("dev", "chat", "settings", "tts")' in stack
    assert '"character_memory.settings_server"' in stack
    assert 'settings.tts_provider' in media
    assert 'selected in {"kokoro", "edge", "gsv"}' in media
    assert '"provider": "qwen3"' in media
    assert '"edge"' in media
    assert '{"value": "edge", "label": "Microsoft Edge TTS (online)"}' in settings_store
    assert '{"value": "gsv", "label": "GSV-TTS-Lite (local)"}' in settings_store
    assert '/v1/providers' in settings_server
    assert "health check did not pass" in settings_server
    assert "tts-health-status" in settings_js
    assert "option.disabled" in settings_js
    assert "zh-CN-XiaoxiaoNeural" in settings_store
    assert '"http://127.0.0.1:9013"' in media
    assert 'f"{qwen3_base}/v1/tts"' in media
    assert 'http://127.0.0.1:8003/settings' in chat
    assert 'http://127.0.0.1:8003/settings' in lab
