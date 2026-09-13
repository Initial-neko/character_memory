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

    result = store.save_values({"tts_provider": "kokoro", "tts_voice": "zf_003"})

    assert result["changed"] is True
    assert Path(result["backup"]).is_file()
    text = config.read_text(encoding="utf-8")
    assert "# user comment" in text
    assert 'custom_extension_key: "keep-me"' in text
    assert 'tts_provider: "kokoro"' in text
    assert 'tts_voice: "zf_003"' in text
    settings = load_settings(str(config))
    assert settings.tts_provider == "kokoro"
    assert settings.tts_voice == "zf_003"


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
    app = create_settings_app(str(config), store=store)

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


def test_settings_center_and_formal_tts_wiring_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    stack = Path("src/character_memory/dev_stack.py").read_text(encoding="utf-8")
    media = Path("src/character_memory/media_server.py").read_text(encoding="utf-8")
    chat = Path("src/character_memory/web/index.html").read_text(encoding="utf-8")
    lab = Path("src/character_memory/web/tts_lab.html").read_text(encoding="utf-8")

    assert 'character-settings = "character_memory.settings_server:main"' in pyproject
    assert "127.0.0.1:8003/health" in stack
    assert 'choices=("dev", "chat", "settings", "tts")' in stack
    assert '"character_memory.settings_server"' in stack
    assert 'settings.tts_provider' in media
    assert '"provider": "kokoro"' in media
    assert 'http://127.0.0.1:8003/settings' in chat
    assert 'http://127.0.0.1:8003/settings' in lab
