from character_memory.config import Settings


def test_default_text_and_vision_models_use_opencode_go_ids():
    settings = Settings()
    assert settings.base_url == "https://opencode.ai/zen/go/v1"
    assert settings.chat_model == "deepseek-flash"
    assert settings.vision_model == "deepseek-v4-flash-vision-exp"
    assert settings.media_max_bytes == 8 * 1024 * 1024
