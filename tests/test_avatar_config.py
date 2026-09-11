from character_memory.config import Settings


def test_avatar_search_settings_have_safe_defaults():
    settings = Settings()

    assert settings.search_provider == "brave"
    assert settings.search_api_key == ""
    assert settings.search_country == "ALL"
    assert settings.search_language == "zh"
    assert settings.search_safe_search == "strict"
    assert settings.avatar_dir == ""
    assert settings.avatar_max_bytes == 8 * 1024 * 1024
