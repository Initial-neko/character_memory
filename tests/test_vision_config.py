from character_memory.app import build_model
from character_memory.config import Settings


def test_default_multimodal_model_reuses_chat_model():
    settings = Settings(api_key="test-key")
    assert settings.base_url == "https://opencode.ai/zen/go/v1"
    assert settings.chat_model == "deepseek-flash"
    assert settings.vision_model is None
    model = build_model(settings)
    try:
        assert model.model == "deepseek-flash"
        assert model.vision_model == "deepseek-flash"
    finally:
        model.close()
    assert settings.media_max_bytes == 8 * 1024 * 1024


def test_explicit_vision_model_override_remains_supported():
    settings = Settings(
        api_key="test-key",
        chat_model="deepseek-flash",
        vision_model="legacy-vision-model",
    )
    model = build_model(settings)
    try:
        assert model.model == "deepseek-flash"
        assert model.vision_model == "legacy-vision-model"
    finally:
        model.close()


def test_empty_vision_model_also_falls_back_to_chat_model():
    settings = Settings(api_key="test-key", vision_model="")
    model = build_model(settings)
    try:
        assert model.vision_model == settings.chat_model
    finally:
        model.close()
