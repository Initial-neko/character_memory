from character_memory.app import build_model
from character_memory.config import Settings


def test_default_multimodal_model_reuses_chat_model(tmp_path):
    settings = Settings(api_key="test-key", db_path=str(tmp_path / "usage.db"))
    assert settings.base_url == "https://opencode.ai/zen/go/v1"
    assert settings.chat_model == "deepseek-v4.1-flash"
    assert settings.vision_model is None
    model = build_model(settings)
    try:
        assert model.model == "deepseek-v4.1-flash"
        assert model.vision_model == "deepseek-v4.1-flash"
    finally:
        model.close()
    assert settings.media_max_bytes == 8 * 1024 * 1024


def test_explicit_vision_model_override_remains_supported(tmp_path):
    settings = Settings(
        api_key="test-key",
        db_path=str(tmp_path / "usage.db"),
        chat_model="deepseek-flash",
        vision_model="legacy-vision-model",
    )
    model = build_model(settings)
    try:
        assert model.model == "deepseek-v4.1-flash"
        assert model.vision_model == "legacy-vision-model"
    finally:
        model.close()


def test_empty_vision_model_also_falls_back_to_chat_model(tmp_path):
    settings = Settings(api_key="test-key", vision_model="", db_path=str(tmp_path / "usage.db"))
    model = build_model(settings)
    try:
        assert model.vision_model == settings.chat_model
    finally:
        model.close()
