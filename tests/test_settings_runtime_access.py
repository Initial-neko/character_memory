from __future__ import annotations

from types import SimpleNamespace

import pytest

from character_memory.config import Settings, runtime_setting


def test_runtime_setting_is_strict_for_formal_settings():
    settings = Settings()
    assert runtime_setting(settings, "voice_silence_ms", 123) == 900

    with pytest.raises(AttributeError):
        runtime_setting(settings, "voice_silence_milliseconds", 123)


def test_runtime_setting_allows_explicit_legacy_adapter_default():
    adapter = SimpleNamespace()
    assert runtime_setting(adapter, "voice_silence_ms", 900) == 900

    adapter.voice_silence_ms = 1200
    assert runtime_setting(adapter, "voice_silence_ms", 900) == 1200
