from character_memory.config import Settings, load_settings


def test_avatar_search_settings_have_safe_defaults():
    settings = Settings()

    assert settings.search_provider == "searchapi"
    assert settings.search_api_key == ""
    assert settings.search_country == "jp"
    assert settings.search_language == "zh-cn"
    assert settings.search_safe_search == "strict"
    assert settings.avatar_dir == ""
    assert settings.avatar_max_bytes == 8 * 1024 * 1024


def test_searchapi_environment_key_is_optional_override(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        'search_provider: "searchapi"\nsearch_api_key: "yaml-key"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("SEARCHAPI_API_KEY", "env-key")
    settings = load_settings(str(config))

    assert settings.search_provider == "searchapi"
    assert settings.search_api_key == "env-key"


def test_brave_environment_key_remains_provider_specific(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        'search_provider: "brave"\nsearch_api_key: "yaml-key"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("SEARCHAPI_API_KEY", "wrong-provider")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    settings = load_settings(str(config))

    assert settings.search_provider == "brave"
    assert settings.search_api_key == "brave-key"
